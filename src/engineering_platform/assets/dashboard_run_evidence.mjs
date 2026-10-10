/* Stored, scoped evidence only. This renderer has no network or storage effects. */
const activeViews = new WeakMap();

export function syncStoredRunEvidence(container, evidence, recovery, options) {
  const previous = activeViews.get(container);
  const signature = evidence ? JSON.stringify([options.scope, options.locale, evidence, recovery]) : null;
  if (previous?.signature === signature) return;
  for (const node of container.querySelectorAll("[data-run-evidence]")) node.remove();
  activeViews.delete(container);
  if (!evidence) return;
  container.append(...renderStoredRunEvidence(evidence, recovery, options));
  // Ephemeral presentation identity only; never localStorage/sessionStorage.
  // Identical polling snapshots preserve native focus, open details and filter.
  activeViews.set(container, { signature });
}

export function renderStoredRunEvidence(evidence, recovery, { document, t, card, reviewerLabel }) {
  // Evidence is literal text. Do not inherit filesystem/model URL linkification.
  const field = (label, text) => {
    const node = document.createElement("p"), name = document.createElement("span"), output = document.createElement("span");
    node.className = "field"; name.className = "label"; name.textContent = label;
    output.textContent = text; node.append(name, output); return node;
  };
  const unknown = () => t("run_evidence.not_recorded");
  const value = (item) => item === null || item === undefined || item === "" ? unknown()
    : Array.isArray(item) ? item.join("\n") || unknown() : String(item);
  const state = (item) => {
    const allowed = new Set(["SELECTED", "SKIPPED", "PROPOSED", "ACCEPTED", "REJECTED", "DEFERRED", "IMPLEMENTED", "VERIFIED", "PREPARED", "CREATE_UNCERTAIN", "RECONCILED", "AVAILABLE", "RECOVERY_AVAILABLE", "RECOVERY_STARTING", "RECOVERY_IN_PROGRESS", "RECOVERED", "EXHAUSTED", "PRECHECK_FAILED", "AMBIGUOUS", "BLOCKED", "FAILED", "RECORDED", "NOT_STARTED", "NOT_RECORDED", "UNAVAILABLE"]);
    return t(`run_evidence.state.${allowed.has(item) ? item.toLowerCase() : "not_recorded"}`);
  };
  const selectionReasons = new Set(["complete_context_overflow_at_dispatch", "no_relevant_question_or_consumer", "invalid_specialist_requests", "ambiguous_specialist_requests", "selected", "no_consumer", "no_primary_consumer_for_lifecycle", "irrelevant_task_paths", "capability_unqualified", "capacity_unknown", "mandatory_controls_assurance_and_repair_reserved", "finite_optional_allowance_exhausted", "complete_context_overflow", "missing_snapshot_path", "overlapping_question", "overlapping_or_ambiguous_question", "invalid_question_contract"]);
  const reason = (code) => selectionReasons.has(code) ? t(`run_evidence.reason.${code}`) : value(code);
  const detail = (title, fields, identity) => {
    const node = document.createElement("details"); node.className = "prompt-detail-field";
    const summary = document.createElement("summary"); summary.textContent = title;
    node.append(summary, ...fields); if (identity) node.dataset.evidenceId = identity;
    return node;
  };
  const observation = (item) => (evidence?.specialists?.invocations || []).find((entry) => entry.request_id === item.request_id);
  const binding = (item) => [
    field(t("run_evidence.run"), value(item.run_id || evidence?.run_id)),
    field(t("run_evidence.request"), value(item.request_id)),
    field(t("run_evidence.invocation"), value(item.invocation_id || observation(item)?.invocation_id)),
    field(t("run_evidence.invocation_result"), observation(item)?.state === "COMPLETED"
      ? t("run_evidence.completed_result") : observation(item)?.state === "UNKNOWN" ? t("run_evidence.unknown_result") : unknown()),
    field(t("run_evidence.consumer"), value(item.consumer)),
    field(t("run_evidence.candidate"), value(item.candidate_sha)),
    field(t("run_evidence.profile"), value(item.profile_digest)),
    field(t("run_evidence.source"), value(item.source_digest)),
  ];
  const specialists = evidence?.specialists || {}, findings = specialists.findings || [];
  const selections = [...(specialists.decisions || []), ...(specialists.dispatch_skips || [])];
  const selectionFields = [field(t("run_evidence.stored_only"), t("run_evidence.no_execution")),
    field(t("run_evidence.model_starts"), value(specialists.actual_model_invocation_count)),
    field(t("run_evidence.model_evidence"), state(specialists.invocation_evidence_state)),
    field(t("run_evidence.reservations"), value(specialists.reserved_invocation_count)),
    field(t("run_evidence.results"), value(specialists.completed_invocation_count)),
    field(t("run_evidence.uncertain"), value(specialists.uncertain_invocation_count))];
  for (const item of selections) selectionFields.push(detail(
    `${reviewerLabel(item.reviewer)} · ${state(item.kind === "SKIP" ? "SKIPPED" : item.payload?.status)}`,
    [field(t("detail.specialist_review"), reviewerLabel(item.reviewer)),
      field(t("detail.outcome"), state(item.kind === "SKIP" ? "SKIPPED" : item.payload?.status)),
      field(t("run_evidence.question"), value(item.payload?.question)),
      field(t("run_evidence.reason"), reason(item.payload?.reason)),
      field(t("run_evidence.source_paths"), value(item.payload?.paths)),
      field(t("run_evidence.risk"), ["LOW", "NORMAL", "HIGH"].includes(item.payload?.risk)
        ? t(`run_evidence.risk.${item.payload.risk.toLowerCase()}`) : unknown()),
      field(t("run_evidence.capacity"), unknown()), ...binding(item)], item.request_id,
  ));
  if (!selections.length) selectionFields.push(field(t("run_evidence.evidence"), state(specialists.state)));
  const selectionCard = card(t("run_evidence.specialists"), selectionFields, true, "prompt-detail-card--run-evidence");
  selectionCard.dataset.runEvidence = "selection";

  const findingFields = [], filter = document.createElement("select"), label = document.createElement("label");
  label.textContent = t("run_evidence.filter"); filter.setAttribute("aria-label", t("run_evidence.filter"));
  for (const item of ["ALL", "PROPOSED", "ACCEPTED", "REJECTED", "DEFERRED", "IMPLEMENTED", "VERIFIED"]) {
    const option = document.createElement("option"); option.value = item;
    option.textContent = item === "ALL" ? t("run_evidence.all") : state(item); filter.append(option);
  }
  label.append(filter); findingFields.push(label);
  for (const item of findings) {
    const history = (specialists.history || []).filter((event) => event.payload?.finding_id === item.id);
    const node = detail(`${state(item.disposition)} · ${value(item.summary)}`, [
      field(t("detail.outcome"), state(item.disposition)),
      field(t("run_evidence.advice"), value(item.summary)),
      field(t("run_evidence.finding"), value(item.id)), field(t("run_evidence.reason"), value(item.reason)),
      ...binding(item), field(t("run_evidence.evidence"), value(item.evidence_ref)),
      field(t("run_evidence.path"), value(item.path)),
      field(t("detail.changed_files"), value(item.changed_paths)),
      field(t("run_evidence.result_candidate"), value(item.result_candidate_sha)),
      field(t("run_evidence.verification_binding"), item.disposition !== "VERIFIED" ? t("run_evidence.not_verified")
        : !evidence?.recorded_candidate_sha ? unknown()
        : item.result_candidate_sha === evidence.recorded_candidate_sha
          ? t("run_evidence.recorded_candidate") : t("run_evidence.historical_candidate")),
      field(t("run_evidence.controls"), value(item.control_refs)),
      field(t("run_evidence.validation_profile"), value(item.validation_profile_digest)),
      ...history.map((event) => field(state(event.payload?.disposition), value(event.payload?.reason || event.payload?.result_candidate_sha))),
    ], item.id);
    node.dataset.disposition = item.disposition; findingFields.push(node);
  }
  filter.addEventListener("change", () => {
    for (const node of findingFields) if (node.dataset?.disposition)
      node.hidden = filter.value !== "ALL" && filter.value !== node.dataset.disposition;
  });
  if (!findings.length) findingFields.push(field(t("run_evidence.evidence"),
    specialists.available === true ? t("run_evidence.no_findings") : unknown()));
  findingFields.push(field(t("run_evidence.duplicates"), value(specialists.duplicate_observations)));
  const findingCard = card(t("run_evidence.findings"), findingFields, true, "prompt-detail-card--run-evidence");
  findingCard.dataset.runEvidence = "findings";

  const publication = evidence?.publication || {};
  const publicationCard = card(t("run_evidence.publication_recovery"), [
    field(t("run_evidence.publication"), state(publication.status)),
    field(t("run_evidence.run"), value(publication.run_id)),
    field(t("run_evidence.branch"), value(publication.branch)),
    field(t("run_evidence.candidate"), value(publication.candidate_sha)),
    field(t("run_evidence.pull_request"), value(publication.pull_request)),
    field(t("run_evidence.provider_recovery"), state(recovery?.state)),
    field(t("run_evidence.original_invocation"), value(recovery?.triggering_invocation_id)),
    field(t("run_evidence.replacement_invocation"), value(recovery?.replacement_invocation_id)),
    field(t("run_evidence.last_observed"), value(recovery?.last_observed_at)),
    field(t("run_evidence.reason"), String(recovery?.diagnostic_code || "").startsWith("LAUNCH_NOT_STARTED:")
      ? t("run_evidence.launch_not_started") : recovery?.diagnostic_code === "PRECHECK_FAILED"
        ? t("run_evidence.state.precheck_failed") : value(recovery?.diagnostic_code)),
    field(t("run_evidence.repository_binding"), evidence?.current_repository_binding === "UNBOUND"
      ? t("run_evidence.binding_unbound") : evidence?.current_repository_binding === "BOUND"
        ? t("run_evidence.binding_recorded") : unknown()),
    field(t("run_evidence.execution_binding"), evidence?.recorded_execution_authorized === true ? t("run_evidence.authorization_recorded")
      : evidence?.recorded_execution_authorized === false ? t("run_evidence.authorization_revoked") : unknown()),
    field(t("run_evidence.stored_only"), t("run_evidence.uncertainty")),
  ], true, "prompt-detail-card--run-evidence");
  publicationCard.dataset.runEvidence = "publication";
  return [selectionCard, findingCard, publicationCard];
}
