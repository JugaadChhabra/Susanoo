"""Resolve the build specification's T01-T40 against executed, not merely collected, tests."""

from fnmatch import fnmatchcase
from pathlib import Path
from xml.etree import ElementTree


# Every selector is required. A missing test, skip, error or failure is not acceptance.
REQUIREMENTS = {
    "T01": ["test_runner_local.test_m2_correct_agent_passes_every_case",
            "test_runtime_integration.test_real_runtime_round_trip*"],
    "T02": ["test_runner_local.test_m2_false_completion_fails_from_independent_evidence"],
    "T03": ["test_evaluator.test_wrong_amount_and_wrong_account", "test_evaluator.test_duplicate_is_proven*",
            "test_evaluator.test_forbidden_change_on_balance"],
    "T04": ["test_runner_local.test_m2_observer_outage_is_unknown_not_pass"],
    "T05": ["test_runner_local.test_timeout_after_write_is_uncertain_then_reconciled"],
    "T06": ["test_journal.test_crash*", "test_restart_capacity.test_killed_runner_restarts*"],
    "T07": ["test_platform.test_launch_idempotency_and_conflict", "test_platform.test_event_dedupe*"],
    "T08": ["test_runner_local.test_m2_correct_agent_passes_every_case", "test_capacity.test_twenty_case_protocol*"],
    "T09": ["test_platform.test_cancel_stops_dispatch*", "test_runner_local.test_cancellation_starts_no_new_action"],
    "T10": ["test_workflows.test_exclusive_fixture_remains_quarantined*", "test_runner_local.test_cleanup_failure*"],
    "T11": ["test_platform.test_immutable_versions_approval_and_separate_reviewer"],
    "T12": ["test_hidden_boundaries.test_hidden_answers_absent*", "test_runner_local.test_agent_never_receives*"],
    "T13": ["test_platform.test_database_rejects_cross_workspace_version_links",
            "test_hardening.test_runner_artifact_scope*"],
    "T14": ["test_browser_adapter.test_storage_state_secrets*",
            "test_tool_sources.test_each_source_contains_nothing_secret*"],
    "T15": ["test_cli_ci.test_compare_exact_transitions", "test_cli_ci.test_compare_changed_criteria*"],
    "T16": ["test_workflows.test_waiver_expiry_changes_acceptance_not_machine_verdict"],
    "T17": ["test_platform.test_waiver_preserves_verdict_and_four_exports_agree", "test_runtime_exports.*"],
    "T18": ["test_hardening.test_backup_restore_reapplies_newer_deletion*", "test_platform.test_delete_revokes*"],
    "T19": ["test_source_intake.test_git_acquisition*", "test_source_intake.test_archive_rejections*",
            "test_github_boundary.test_source_read_grant*", "test_platform_source_jobs.*",
            "test_source_runtime.test_actual_local_source_import*"],
    "T20": ["test_hardening.test_signed_callback_dedupe_and_repository_boundary",
            "test_github_boundary.test_global_app_webhook*"],
    "T21": ["test_agent_transports.test_suite_runs_over_every_transport*",
            "test_protocol_connectors.test_suite_runs_over_protocol_connectors*", "test_platform_bindings.*"],
    "T22": ["test_agent_transports.test_streams_must_complete*", "test_protocol_connectors.test_missing_callback*"],
    "T23": ["test_protocol_connectors.test_mcp_tool_only*", "test_protocol_connectors.test_a2a_version_mismatch*",
            "test_protocol_connectors.test_grpc_descriptor_review_and_version_mismatch"],
    "T24": ["test_browser_adapter.test_multi_turn_in_a_dedicated_context_per_session",
            "test_browser_adapter.test_ui_success_never_becomes_a_pass*", "test_provider_wires.test_real_playwright*"],
    "T25": ["test_stateful_simulator.*"],
    "T26": ["test_fidelity.test_mismatched_prerequisites*", "test_fidelity.test_normalizers*"],
    "T27": ["test_fidelity.test_seeded_discrepancies*", "test_fidelity.test_declared_simplification*"],
    "T28": ["test_fidelity.test_fresh_pairs_attribute_only_after_intervention*",
            "test_advanced_runtime.*fresh_agent_pair*",
            "test_advanced_runtime.*two_by_two*"],
    "T29": ["test_fidelity.test_reduction_keeps*", "test_fidelity.test_reduction_respects_its_budget"],
    "T30": ["test_evaluator.test_calibration_catches_missed_defects_and_invalid_controls",
            "test_platform_science_jobs.test_actual_claim_calibrates*"],
    "T31": ["test_fidelity.test_full_repair_is_accepted", "test_fidelity.test_partial_repair*",
            "test_agent_repair.test_manual_patch_flips*", "test_agent_repair.test_evaluator_patch*",
            "test_source_runtime.test_actual_worktree_repair*"],
    "T32": ["test_publication.*", "test_agent_repair.test_duplicate_publication*"],
    "T33": ["test_incidents_dependencies.test_change_marks*", "test_advanced_reports.test_automatic_run_budget*"],
    "T34": ["test_passive.test_passive_traces_reach*", "test_incidents_dependencies.test_incident_to_draft*"],
    "T35": ["test_collision.test_read_before_write_barrier*",
            "test_collision.test_normal_suites_keep_namespace_isolation",
            "test_platform_science_jobs.test_actual_claim_detects_shared_state_collision"],
    "T36": ["test_probing.test_concurrent_workers_and_runs_never_exceed_the_cap",
            "test_probing.test_strategies_compare_only_on_equal_scope_budget_and_checker",
            "test_advanced_runtime.test_actual_probe_compares_equal_budget_strategies"],
    "T37": ["test_media.test_media_session_assembles*", "test_media.test_partial_agent_audio*",
            "test_media.test_transcript_only_run_refuses_audio_metrics",
            "test_platform_science_jobs.test_actual_claim_runs_local_synthetic_media*"],
    "T38": ["test_semantic.*"],
    "T39": ["test_workflows.test_default_deny_rls_and_repeat_migrations", "test_hardening.test_backup_restore*",
            "test_restart_capacity.test_killed_runner_restarts*"],
    "T40": ["test_capacity.test_capacity_ten_thousand_attempts_real_http",
            "test_restart_capacity.test_capacity_benchmark*"],
}


def read_results(paths: list[Path]):
    results = []
    for path in paths:
        if not path.is_file():
            continue
        root = ElementTree.parse(path).getroot()
        for case in root.iter("testcase"):
            name = case.get("classname", "").rsplit(".", 1)[-1] + "." + case.get("name", "")
            status = next((kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None), "passed")
            results.append({"test": name, "status": status, "report": str(path)})
    return results


def evaluate_matrix(paths: list[Path]):
    results = read_results(paths)
    matrix = {}
    for identifier, selectors in REQUIREMENTS.items():
        missing, evidence = [], []
        for selector in selectors:
            matched = [result for result in results if fnmatchcase(result["test"], selector)]
            if not matched:
                missing.append(selector)
            evidence.extend(matched)
        matrix[identifier] = {
            "status": ("passed" if not missing and all(row["status"] == "passed" for row in evidence)
                       else "not_accepted"),
            "missing_selectors": missing, "evidence": evidence,
        }
    return matrix
