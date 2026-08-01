import Darwin
import Dispatch
import Foundation

let port: UInt16 = 8765
let loopbackAddress = "127.0.0.1"
let repository = ProcessInfo.processInfo.environment["DJCONNECT_ENGINEERING_REPOSITORY"]
    ?? "\(NSHomeDirectory())/Documents/GitHub/djconnect"
let python = ProcessInfo.processInfo.environment["DJCONNECT_ENGINEERING_PYTHON"]
    ?? "\(NSHomeDirectory())/.platformio/penv/bin/python3"

func tailscaleAddress() -> String? {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
    process.arguments = ["tailscale", "ip", "-4"]
    process.standardOutput = output
    do {
        try process.run()
        process.waitUntilExit()
    } catch {
        return nil
    }
    guard process.terminationStatus == 0,
          let value = String(data: output.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)
              .split(whereSeparator: \.isNewline).first
    else { return nil }
    return String(value)
}

func socketHandle() -> Int32 {
    let handle = socket(AF_INET, SOCK_STREAM, 0)
    guard handle >= 0 else { return handle }
    var noSignal: Int32 = 1
    setsockopt(handle, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
    return handle
}

func listener(address: String) -> Int32? {
    let handle = socketHandle()
    guard handle >= 0 else { return nil }
    var reuse: Int32 = 1
    setsockopt(handle, SOL_SOCKET, SO_REUSEADDR, &reuse, socklen_t(MemoryLayout<Int32>.size))
    var endpoint = sockaddr_in()
    endpoint.sin_family = sa_family_t(AF_INET)
    endpoint.sin_port = port.bigEndian
    guard inet_pton(AF_INET, address, &endpoint.sin_addr) == 1 else { close(handle); return nil }
    let result = withUnsafePointer(to: &endpoint) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(handle, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    guard result == 0, listen(handle, 32) == 0 else { close(handle); return nil }
    return handle
}

func backend() -> Int32? {
    let handle = socketHandle()
    guard handle >= 0 else { return nil }
    var endpoint = sockaddr_in()
    endpoint.sin_family = sa_family_t(AF_INET)
    endpoint.sin_port = port.bigEndian
    guard inet_pton(AF_INET, loopbackAddress, &endpoint.sin_addr) == 1 else { close(handle); return nil }
    let result = withUnsafePointer(to: &endpoint) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            Darwin.connect(handle, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    guard result == 0 else { close(handle); return nil }
    return handle
}

func relay(from source: Int32, to destination: Int32) {
    var bytes = [UInt8](repeating: 0, count: 32_768)
    while true {
        let received = recv(source, &bytes, bytes.count, 0)
        guard received > 0 else { shutdown(destination, SHUT_WR); return }
        var sent = 0
        while sent < received {
            let result = bytes.withUnsafeBytes {
                send(destination, $0.baseAddress!.advanced(by: sent), received - sent, 0)
            }
            guard result > 0 else { return }
            sent += result
        }
    }
}

func superviseWatcher() {
    DispatchQueue.global(qos: .utility).async {
        while true {
            let watcher = Process()
            watcher.executableURL = URL(fileURLWithPath: "/bin/zsh")
            watcher.arguments = ["-lc", "cd \(repository) && exec \(python) -m tools.engineering.inbox_watcher run --repo \(repository)"]
            do {
                try watcher.run()
                watcher.waitUntilExit()
            } catch { }
            Thread.sleep(forTimeInterval: 5)
        }
    }
}

superviseWatcher()

while true {
    guard let address = tailscaleAddress(), let server = listener(address: address) else {
        Thread.sleep(forTimeInterval: 5)
        continue
    }
    while true {
        var remote = sockaddr()
        var length = socklen_t(MemoryLayout<sockaddr>.size)
        let client = accept(server, &remote, &length)
        guard client >= 0 else { continue }
        DispatchQueue.global(qos: .userInitiated).async {
            guard let target = backend() else { close(client); return }
            let group = DispatchGroup()
            group.enter(); DispatchQueue.global().async { relay(from: client, to: target); group.leave() }
            group.enter(); DispatchQueue.global().async { relay(from: target, to: client); group.leave() }
            group.wait()
            close(client)
            close(target)
        }
    }
}
