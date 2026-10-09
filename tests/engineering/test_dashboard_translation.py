from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from engineering_platform import dashboard_translation


class DashboardTranslationTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_translation._cache.clear()

    def test_dynamic_evidence_is_translated_by_the_managed_codex_runtime(self) -> None:
        source = "No changed behavior or executable test surface exists."
        translated = "Er is geen gewijzigd gedrag of uitvoerbaar testoppervlak."
        event = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps({"translations": [translated]})},
        })
        completed = subprocess.CompletedProcess(("codex",), 0, event, "")
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=completed) as invoke:
            self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])
        arguments = invoke.call_args.args[1]
        self.assertIn("read-only", arguments)
        self.assertIn("--output-schema", arguments)
        self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])
        self.assertEqual(invoke.call_count, 1)

    def test_english_evidence_is_returned_without_a_provider_call(self) -> None:
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke") as invoke:
            self.assertEqual(dashboard_translation.translate("en", ["Recorded validation passed."]), ["Recorded validation passed."])
        invoke.assert_not_called()

    def test_request_is_bounded_and_rejects_unknown_locales(self) -> None:
        with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "LOCALE_INVALID"):
            dashboard_translation.translate("pt", ["text"])
        with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "REQUEST_INVALID"):
            dashboard_translation.translate("nl", ["x" * (dashboard_translation.MAX_TEXT_LENGTH + 1)])

    def test_long_evidence_and_expanded_translation_are_preserved(self) -> None:
        source = "évidence with unicode and multiple paragraphs\n\n" + ("complete source text " * 70)
        translated = "vertaalde evidence\n\n" + ("volledige vertaalde tekst " * 120)
        event = json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps({"translations": [translated]})},
        })
        completed = subprocess.CompletedProcess(("codex",), 0, event, "")
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=completed):
            self.assertEqual(dashboard_translation.translate("nl", [source]), [translated])

    def test_provider_failure_is_a_safe_display_failure(self) -> None:
        with patch(
            "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
            side_effect=OSError("runtime unavailable"),
        ):
            with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "UNAVAILABLE"):
                dashboard_translation.translate("nl", ["Recorded validation passed."])

    def test_non_object_events_and_invalid_payloads_fail_safely_without_caching(self) -> None:
        for output in ("[]", "null", "42", "true", '"event"', "{broken", "[" * 1100 + "]" * 1100, json.dumps({
            "type": "item.completed", "item": {"type": "agent_message", "text": "{}"},
        })):
            with self.subTest(output=output), patch(
                "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
                return_value=subprocess.CompletedProcess(("codex",), 0, output, ""),
            ):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "^DASHBOARD_TRANSLATION_OUTPUT_INVALID$"):
                    dashboard_translation.translate("nl", ["Source evidence"])
                self.assertEqual(dashboard_translation._cache, {})

    def test_output_requires_valid_cardinality_types_and_translation_lengths(self) -> None:
        for translations in ([], ["one", "two"], [None], [2], [" "], ["x" * (dashboard_translation.MAX_TRANSLATION_LENGTH + 1)]):
            event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"translations": translations})}})
            with self.subTest(translations=repr(translations)[:80]), patch(
                "engineering_platform.dashboard_translation.CodexCliProvider.invoke",
                return_value=subprocess.CompletedProcess(("codex",), 0, event, ""),
            ):
                with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "OUTPUT_INVALID"):
                    dashboard_translation.translate("nl", ["Source evidence"])
                self.assertEqual(dashboard_translation._cache, {})

    def test_excessively_nested_final_message_fails_safely(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "[" * 1100 + "]" * 1100}})
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=subprocess.CompletedProcess(("codex",), 0, event, "")):
            with self.assertRaisesRegex(dashboard_translation.DashboardTranslationError, "OUTPUT_INVALID"):
                dashboard_translation.translate("nl", ["Source evidence"])

    def test_unrelated_non_object_events_do_not_hide_a_valid_final_message(self) -> None:
        event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"translations": ["Bronbewijs"]})}})
        with patch("engineering_platform.dashboard_translation.CodexCliProvider.invoke", return_value=subprocess.CompletedProcess(
            ("codex",), 0, "null\n" + event + "\n[]\nnot json", "",
        )):
            self.assertEqual(dashboard_translation.translate("nl", ["Source evidence"]), ["Bronbewijs"])

    def test_current_response_retains_valid_translations_during_concurrent_cache_eviction(self) -> None:
        dashboard_translation._cache[("nl", "Earlier source")] = "Eerdere bron"

        def translate_while_another_request_fills_cache(*_: object) -> tuple[str, ...]:
            with dashboard_translation._lock:
                dashboard_translation._cache.clear()
                for index in range(dashboard_translation.MAX_CACHE_ENTRIES):
                    dashboard_translation._cache[("nl", f"other-{index}")] = f"ander-{index}"
            return ("Nieuwe bron",)

        with patch.object(dashboard_translation, "_translate_missing", side_effect=translate_while_another_request_fills_cache):
            self.assertEqual(dashboard_translation.translate("nl", ["Earlier source", "New source"]), ["Eerdere bron", "Nieuwe bron"])
        self.assertEqual(len(dashboard_translation._cache), dashboard_translation.MAX_CACHE_ENTRIES)

    def test_public_chat_and_translation_keep_user_content_off_command_line(self):
        """Real child-process transport, with only its external model response fixed."""
        import os
        import sys
        import tempfile
        from pathlib import Path
        from engineering_platform.codex_chat import respond_with_context
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prefix = root / 'provider'; (prefix / 'bin').mkdir(parents=True)
            capture = root / 'transport.jsonl'
            launcher = prefix / 'bin/codex'
            launcher.write_text('#!' + sys.executable + '\n' +
                'import sys,json\nfrom pathlib import Path\n' +
                'content=sys.stdin.read()\n' +
                'with Path(' + repr(str(capture)) + ').open("a") as log: log.write(json.dumps({"argv":sys.argv[1:],"stdin":content})+"\\n")\n' +
                'answer=json.dumps({"translations":["Veilige projectie"]}) if "--output-schema" in sys.argv else "Veilig advies"\n' +
                'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":answer}}))\n')
            launcher.chmod(0o700)
            content = '--dangerously-bypass-approvals-and-sandbox; $(touch sentinel)\n"untrusted" é'
            with patch.dict(os.environ, {'EP_MANAGED_CODEX_CLI_PREFIX': str(prefix)}):
                self.assertEqual(respond_with_context(content, {'evidence': content}), 'Veilig advies')
                self.assertEqual(dashboard_translation.translate('nl', [content]), ['Veilige projectie'])
            records = [json.loads(line) for line in capture.read_text().splitlines()]
            self.assertEqual(len(records), 2)
            for record in records:
                self.assertEqual(record['argv'][-1], '-', 'the fixed stdin prompt marker must be literal')
                self.assertIn('read-only', record['argv'])
                self.assertIn('--ignore-user-config', record['argv'])
                self.assertFalse(any(content in argument for argument in record['argv']))
                self.assertIn('untrusted', record['stdin'])
                self.assertIn('é', record['stdin'])
                self.assertIn('$(touch sentinel)', record['stdin'])
            self.assertFalse((root / 'sentinel').exists())

    def test_genuine_native_cli_reads_console_prompts_from_stdin(self):
        import http.server
        import os
        import shutil
        import sys
        import tempfile
        import threading
        from pathlib import Path
        from engineering_platform.codex_chat import respond_with_context
        calls = []
        class Model(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_CONNECT(self):
                self.send_error(403, 'External network is forbidden in this fixture')
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(payload)
                answer = 'Veilig advies' if len(calls) == 1 else json.dumps({'translations': ['Veilige projectie']})
                item = {'id':'message','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':answer}]}
                events = [{'type':'response.created','response':{'id':'fixture','status':'in_progress','output':[]}},
                    {'type':'response.output_item.added','output_index':0,'item':item},
                    {'type':'response.output_item.done','output_index':0,'item':item},
                    {'type':'response.completed','response':{'id':'fixture','status':'completed','output':[item],
                    'usage':{'input_tokens':20,'output_tokens':20,'total_tokens':40}}}]
                data = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Model)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(thread.join);self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);prefix=root/'provider';(prefix/'bin').mkdir(parents=True)
            home=root/'cli-home';home.mkdir()
            endpoint='http://127.0.0.1:'+str(server.server_port)
            native=shutil.which('codex')
            version=subprocess.run((native,'--version'),capture_output=True,text=True,check=True)
            self.assertEqual(version.stdout.strip(),'codex-cli 0.160.1')
            launcher=prefix/'bin/codex'
            launcher.write_text('#!'+sys.executable+'\nimport os,sys\n'+
                'native='+repr(native)+'\n'+
                'config='+repr(['-c','model_provider="fixture"','-c',
                    'model_providers.fixture={name="fixture",base_url="'+endpoint+'/v1",wire_api="responses",requires_openai_auth=false}'])+'\n'+
                'os.execv(native,[native,*sys.argv[1:],*config])\n')
            launcher.chmod(0o700)
            content='--dangerously-bypass-approvals-and-sandbox; $(touch sentinel) é'
            with patch.dict(os.environ,{'EP_MANAGED_CODEX_CLI_PREFIX':str(prefix),'CODEX_HOME':str(home),
                    'OPENAI_BASE_URL':endpoint+'/v1','OPENAI_API_KEY':'local-fixture-only',
                    'HTTPS_PROXY':endpoint,'HTTP_PROXY':endpoint,'ALL_PROXY':endpoint,'NO_PROXY':'127.0.0.1,localhost'}):
                self.assertEqual(respond_with_context(content,{'evidence':content}),'Veilig advies')
                self.assertEqual(dashboard_translation.translate('nl',[content]),['Veilige projectie'])
            self.assertEqual(len(calls),2,'both genuine native model requests must reach only the local fixture')
            for call in calls:
                self.assertIn(content,json.dumps(call,ensure_ascii=False))
            self.assertFalse((root/'sentinel').exists())
