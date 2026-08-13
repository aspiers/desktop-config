#!/bin/bash

# The sourced reducer consumes globals assigned by this test, but ShellCheck
# analyzes this file without following the dynamically resolved sibling path.
# shellcheck disable=SC1091,SC2034

set -u

here=$(cd "$(dirname "$0")" && pwd)
source "$here/monitor-watcher-state-machine.sh"

failures=0

run_test () {
    local name=$1
    shift
    if ("$@"); then
        printf 'ok - %s\n' "$name"
    else
        printf 'not ok - %s\n' "$name" >&2
        failures=$((failures + 1))
    fi
}

has_action () {
    local wanted=$1 action
    for action in "${SM_ACTIONS[@]}"; do
        [[ $action == "$wanted" ]] && return 0
    done
    return 1
}

has_action_prefix () {
    local wanted=$1 action
    for action in "${SM_ACTIONS[@]}"; do
        [[ $action == "$wanted"* ]] && return 0
    done
    return 1
}

journal_count () {
    local wanted=$1 action count=0
    for action in "${SM_ACTION_JOURNAL[@]}"; do
        [[ $action == "$wanted"* ]] && count=$((count + 1))
    done
    printf '%s\n' "$count"
}

observe () {
    # now key physical external target scope exact current [valid]
    sm_observe "$@"
}

admit_and_complete_application () {
    local now_ms=$1
    [[ $SM_PHASE == APPLY_PENDING ]] || return 1
    sm_application_dispatched "$now_ms" || return 1
    sm_application_done "$((now_ms + 1))" 0
}

admit_finalizer () {
    local now_ms=$1
    [[ $SM_PHASE == FINALIZE_PENDING ]] || return 1
    sm_finalization_dispatched "$now_ms"
}

complete_finalizer_and_revalidate () {
    local now_ms=$1 key=$2 physical=$3 external=$4 profile=$5
    sm_finalization_done "$now_ms" 0 || return 1
    observe "$((now_ms + 1))" "$key" "$physical" "$external" \
        "$profile" external "$profile" "$profile"
}

test_laptop_startup_adopts_baseline () {
    sm_init
    observe 0 laptop p-laptop none celtic internal celtic celtic
    observe 10000 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == QUIESCENT ]] &&
        [[ $SM_DESKTOP_FINALIZED_PROFILE == celtic ]] &&
        has_action 'ADOPT_BASELINE celtic' &&
        [[ $(journal_count APPLY) -eq 0 ]] &&
        [[ $(journal_count FINALIZE) -eq 0 ]]
}

test_genuine_external_plug_applies_and_finalizes_once () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    [[ $SM_PHASE == APPLY_PENDING ]] && has_action_prefix 'APPLY samsung ' || return 1
    admit_and_complete_application 1 || return 1

    observe 1000 active p-samsung known samsung external samsung samsung
    observe 11000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] && has_action_prefix 'FINALIZE ' || return 1
    admit_finalizer 11001 || return 1
    complete_finalizer_and_revalidate 12000 active p-samsung known samsung || return 1

    [[ $SM_PHASE == QUIESCENT ]] &&
        [[ $SM_DESKTOP_FINALIZED_PROFILE == samsung ]] &&
        [[ $(journal_count APPLY) -eq 1 ]] &&
        [[ $(journal_count FINALIZE\ ) -eq 1 ]]
}

test_readiness_after_fast_deadline_needs_no_event () {
    sm_init celtic
    observe 0 missing p-samsung unresolved - - - celtic
    observe 30000 missing p-samsung unresolved - - - celtic
    [[ $SM_PHASE == WAIT_SLOW && $SM_NEXT_TIMER_MS -gt 30000 ]] || return 1

    observe "$SM_NEXT_TIMER_MS" ready p-samsung known samsung external - celtic
    [[ $SM_PHASE == APPLY_PENDING ]] && has_action_prefix 'APPLY samsung '
}

test_edid_loss_retains_candidate_without_fallback () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    admit_and_complete_application 1 || return 1

    observe 1000 missing p-samsung unresolved celtic internal - celtic
    [[ $SM_CANDIDATE_PROFILE == samsung ]] &&
        [[ $SM_PHASE == DISCOVER_FAST ]] &&
        ! has_action_prefix 'APPLY celtic ' || return 1

    observe 2000 active p-samsung known samsung external samsung samsung
    observe 12000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] &&
        [[ $SM_FINALIZATION_PROFILE == samsung ]] &&
        [[ $(journal_count APPLY) -eq 1 ]]
}

test_transient_internal_detection_never_applies_with_external () {
    sm_init celtic
    observe 0 training p-samsung unresolved celtic internal - celtic
    ! has_action_prefix 'APPLY ' && (( SM_NEXT_TIMER_MS >= 0 ))
}

test_unchanged_application_evidence_is_deduplicated () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    admit_and_complete_application 1 || return 1
    observe 2 ready p-samsung known samsung external - celtic
    ! has_action_prefix 'APPLY ' && [[ $(journal_count APPLY) -eq 1 ]]
}

test_event_between_application_admission_and_dispatch_reemits () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    local key=$SM_PENDING_APPLICATION_KEY
    sm_drm_event 1
    [[ $SM_PHASE == APPLY_PENDING ]] || return 1

    observe 1 ready p-samsung known samsung external - celtic
    [[ $SM_PHASE == APPLY_PENDING ]] &&
        has_action "APPLY samsung $key" &&
        [[ $SM_ATTEMPTED_APPLICATION_KEYS != *";$key;"* ]]
}

test_event_between_finalization_admission_and_dispatch_reemits () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] || return 1
    local transaction=$SM_FINALIZATION_ID
    sm_drm_event 10001

    observe 10001 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] &&
        has_action "FINALIZE $transaction samsung" &&
        [[ $SM_FINALIZATION_STATUS == admitted ]]
}

test_interrupted_verification_restarts_full_window () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 9000 invalid p-samsung unresolved - - - celtic 0
    observe 10000 active p-samsung known samsung external samsung samsung
    observe 11000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == VERIFYING ]] && ! has_action_prefix 'FINALIZE '
}

test_unplug_requires_two_samples_spanning_one_second () {
    sm_init samsung
    SM_PHASE=QUIESCENT
    SM_PHYSICAL_TOKEN=p-samsung
    SM_EXTERNAL_INTENT=1
    SM_LAST_EXTERNAL_STATE=known

    observe 0 laptop p-laptop none celtic internal celtic celtic
    observe 1 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == DISCOVER_FAST ]] || return 1
    observe 1000 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == VERIFYING ]] || return 1
    observe 11000 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == FINALIZE_PENDING ]] &&
        [[ $SM_FINALIZATION_PROFILE == celtic ]] &&
        [[ $(journal_count APPLY) -eq 0 ]] &&
        [[ $(journal_count FINALIZE\ ) -eq 1 ]]
}

test_drm_event_resets_unplug_proof () {
    sm_init samsung
    SM_PHASE=QUIESCENT
    SM_PHYSICAL_TOKEN=p-samsung
    SM_EXTERNAL_INTENT=1
    SM_LAST_EXTERNAL_STATE=known

    observe 0 laptop p-laptop none celtic internal celtic celtic
    sm_drm_event 900
    observe 1000 laptop p-laptop none celtic internal celtic celtic
    observe 1900 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == DISCOVER_FAST ]] || return 1
    observe 2000 laptop p-laptop none celtic internal celtic celtic
    [[ $SM_PHASE == VERIFYING ]]
}

test_resume_to_same_profile_skips_finalization () {
    sm_init samsung
    SM_PHASE=QUIESCENT
    SM_PHYSICAL_TOKEN=p-samsung
    SM_STABLE_PROFILE=samsung
    SM_EXTERNAL_INTENT=1
    SM_LAST_EXTERNAL_STATE=known

    sm_drm_event 100
    observe 100 active p-samsung known samsung external samsung samsung
    observe 10100 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == QUIESCENT ]] && has_action 'STABLE samsung' &&
        [[ $(journal_count FINALIZE\ ) -eq 0 ]]
}

test_automatic_x_transition_finalizes_without_apply () {
    sm_init celtic
    SM_PHASE=QUIESCENT
    SM_PHYSICAL_TOKEN=p-laptop
    SM_LAST_EXTERNAL_STATE=none
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] &&
        [[ $(journal_count APPLY) -eq 0 ]]
}

test_connector_rename_keeps_profile_identity () {
    sm_init samsung
    SM_PHASE=QUIESCENT
    SM_PHYSICAL_TOKEN=connector-dp1
    SM_STABLE_PROFILE=samsung
    SM_EXTERNAL_INTENT=1
    SM_LAST_EXTERNAL_STATE=known
    observe 0 same-edid connector-dp2 known samsung external samsung samsung
    observe 10000 same-edid connector-dp2 known samsung external samsung samsung
    [[ $SM_PHASE == QUIESCENT ]] && [[ $(journal_count FINALIZE\ ) -eq 0 ]]
}

test_unknown_external_becomes_terminal_but_health_checked () {
    sm_init celtic
    observe 0 mystery p-mystery unknown - - - celtic
    observe 10000 mystery p-mystery unknown - - - celtic
    [[ $SM_PHASE == UNSUPPORTED ]] && has_action 'UNSUPPORTED mystery' &&
        (( SM_NEXT_TIMER_MS > 10000 ))
}

test_changed_evidence_restarts_fast_discovery_after_slow_wait () {
    sm_init celtic
    observe 0 missing-a p-samsung unresolved - - - celtic
    observe 30000 missing-a p-samsung unresolved - - - celtic
    [[ $SM_PHASE == WAIT_SLOW ]] || return 1

    observe 35000 missing-b p-samsung unresolved - - - celtic
    [[ $SM_PHASE == DISCOVER_FAST ]] &&
        [[ $SM_AGGRESSIVE_DEADLINE_MS -eq 65000 ]]
}

test_fast_timer_does_not_cross_aggressive_deadline () {
    sm_init celtic
    observe 0 missing p-samsung unresolved - - - celtic
    SM_BACKOFF_INDEX=4
    observe 29900 missing p-samsung unresolved - - - celtic
    [[ $SM_PHASE == DISCOVER_FAST ]] && [[ $SM_NEXT_TIMER_MS -eq 30000 ]]
}

test_overdue_fast_recovery_fires_immediately () {
    sm_init celtic
    SM_PHASE=DISCOVER_FAST
    SM_AGGRESSIVE_DEADLINE_MS=30000
    SM_NEXT_TIMER_MS=30000
    sm_recover 31000 test-boot -
    has_action 'SCHEDULE 0'
}

test_restart_from_apply_pending_requires_fresh_observation () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    local key=$SM_PENDING_APPLICATION_KEY
    sm_recover 1 test-boot -
    has_action 'SCHEDULE 0' && ! has_action_prefix 'APPLY ' || return 1

    observe 1 ready p-samsung known samsung external - celtic
    has_action "APPLY samsung $key" &&
        [[ $SM_ATTEMPTED_APPLICATION_KEYS != *";$key;"* ]]
}

test_restart_from_dispatched_application_does_not_repeat () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    sm_application_dispatched 1 || return 1
    sm_recover 2 test-boot -
    observe 2 ready p-samsung known samsung external - celtic
    ! has_action_prefix 'APPLY '
}

test_restart_from_verifying_resets_proof () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    sm_recover 9000 test-boot -
    observe 9000 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == VERIFYING ]] && ! has_action_prefix 'FINALIZE '
}

test_restart_from_finalize_pending_requires_fresh_observation () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] || return 1
    local transaction=$SM_FINALIZATION_ID

    sm_recover 10001 test-boot -
    has_action 'SCHEDULE 0' && ! has_action_prefix 'FINALIZE ' || return 1
    observe 10001 active p-samsung known samsung external samsung samsung
    has_action "FINALIZE $transaction samsung"
}

test_restart_during_finalizer_reattaches () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    admit_finalizer 10001 || return 1
    local transaction=$SM_FINALIZATION_ID
    sm_recover 10002 test-boot running
    [[ $SM_PHASE == FINALIZING ]] && has_action "REATTACH_FINALIZER $transaction" &&
        [[ $(journal_count FINALIZE\ ) -eq 1 ]]
}

test_application_failure_is_terminal_not_silently_deduplicated () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    sm_application_dispatched 1 || return 1
    sm_application_done 2 1 || return 1
    [[ $SM_PHASE == APPLY_FAILED ]] &&
        [[ $SM_APPLICATION_STATUS == failed ]] &&
        has_action 'APPLICATION_FAILED samsung 1'
}

test_finalizer_success_requires_fresh_valid_observation () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    admit_finalizer 10001 || return 1
    sm_finalization_done 11000 0 || return 1
    [[ $SM_DESKTOP_FINALIZED_PROFILE == celtic ]] || return 1

    observe 11001 invalid p-samsung unresolved - - - celtic 0
    [[ $SM_DESKTOP_FINALIZED_PROFILE == celtic ]] || return 1
    observe 12000 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == QUIESCENT ]] &&
        [[ $SM_DESKTOP_FINALIZED_PROFILE == samsung ]] &&
        [[ $(journal_count FINALIZE\ ) -eq 1 ]]
}

test_topology_change_stops_running_finalizer () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    admit_finalizer 10001 || return 1
    local transaction=$SM_FINALIZATION_ID
    observe 11000 laptop p-laptop none celtic internal celtic celtic
    has_action "STOP_FINALIZER $transaction" &&
        [[ $SM_DESKTOP_FINALIZED_PROFILE == celtic ]]
}

test_failed_transition_allows_different_profile_same_topology () {
    sm_init celtic
    SM_PHASE=FINALIZE_FAILED
    SM_FINALIZATION_PROFILE=samsung
    SM_FINALIZATION_STATUS=failed
    SM_PHYSICAL_TOKEN=p-dock
    observe 0 active-aoc p-dock known aoc external aoc aoc
    [[ $SM_PHASE == VERIFYING ]] && [[ $SM_CANDIDATE_PROFILE == aoc ]]
}

test_persistence_round_trip_multiple_application_keys () {
    local tmp
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    sm_init celtic
    sm_remember_application_key '1|samsung|first'
    sm_remember_application_key '1|samsung|second'
    sm_save_state "$tmp" || return 1
    sm_init
    sm_load_state "$tmp" || return 1
    sm_application_key_seen '1|samsung|first' &&
        sm_application_key_seen '1|samsung|second'
}

test_persistence_rejects_truncation_without_partial_mutation () {
    local tmp old_phase
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    sm_init celtic
    old_phase=$SM_PHASE
    printf '%s\n' $'schema_version\t2' $'SM_PHASE\tFINALIZING' > "$tmp"
    ! sm_load_state "$tmp" && [[ $SM_PHASE == "$old_phase" ]]
}

test_persistence_rejects_duplicate_and_arithmetic_payload () {
    local tmp marker
    tmp=$(mktemp)
    marker=${tmp}.executed
    trap 'rm -f "$tmp" "$marker"' RETURN
    sm_init celtic
    sm_save_state "$tmp" || return 1
    printf 'SM_PHASE\tQUIESCENT\n' >> "$tmp"
    ! sm_load_state "$tmp" || return 1

    sm_save_state "$tmp" || return 1
    perl -0pi -e 's/SM_NEXT_TIMER_MS\t[^\n]*/SM_NEXT_TIMER_MS\ta[$(touch ARITH_MARKER)]/' "$tmp"
    perl -pi -e "s#ARITH_MARKER#$marker#" "$tmp"
    ! sm_load_state "$tmp" && [[ ! -e $marker ]]
}

test_new_epoch_gets_new_finalization_id () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    local first=$SM_FINALIZATION_ID

    observe 11000 laptop p-laptop none celtic internal celtic celtic
    observe 12000 laptop p-laptop none celtic internal celtic celtic
    observe 22000 laptop p-laptop none celtic internal celtic celtic
    # The celtic transition is not dispatched; move to a new Samsung epoch.
    observe 23000 active-2 p-samsung-2 known samsung external samsung samsung
    observe 33000 active-2 p-samsung-2 known samsung external samsung samsung
    [[ $SM_FINALIZATION_ID != "$first" ]] &&
        [[ $SM_FINALIZATION_ID == etest-boot-3-* ]]
}

test_invalid_changed_token_preserves_running_finalizer () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    admit_finalizer 10001 || return 1
    local epoch=$SM_PHYSICAL_EPOCH transaction=$SM_FINALIZATION_ID

    observe 11000 torn bogus-token unresolved - - - celtic 0
    [[ $SM_PHASE == FINALIZING ]] &&
        [[ $SM_FINALIZATION_STATUS == running ]] &&
        [[ $SM_PHYSICAL_EPOCH -eq epoch ]] &&
        [[ $SM_FINALIZATION_ID == "$transaction" ]] &&
        ! has_action_prefix 'STOP_FINALIZER '
}

test_invalid_changed_token_preserves_completed_tombstone () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    admit_finalizer 10001 || return 1
    sm_finalization_done 11000 0 || return 1
    local epoch=$SM_PHYSICAL_EPOCH transaction=$SM_FINALIZATION_ID

    observe 11001 torn bogus-token unresolved - - - celtic 0
    [[ $SM_PHASE == FINALIZING ]] &&
        [[ $SM_FINALIZATION_STATUS == result_pending ]] &&
        [[ $SM_PHYSICAL_EPOCH -eq epoch ]] &&
        [[ $SM_FINALIZATION_ID == "$transaction" ]]
}

test_invalid_changed_token_preserves_application_dedup () {
    sm_init celtic
    observe 0 ready p-samsung known samsung external - celtic
    sm_application_dispatched 1 || return 1
    sm_application_done 2 0 || return 1
    local attempted_keys=$SM_ATTEMPTED_APPLICATION_KEYS epoch=$SM_PHYSICAL_EPOCH

    observe 3 torn bogus-token unresolved - - - celtic 0
    [[ $SM_PHYSICAL_EPOCH -eq epoch ]] &&
        [[ $SM_ATTEMPTED_APPLICATION_KEYS == "$attempted_keys" ]] || return 1
    observe 4 ready p-samsung known samsung external - celtic
    ! has_action_prefix 'APPLY '
}

test_failed_away_back_allocates_new_finalization_id () {
    sm_init celtic
    observe 0 active p-samsung known samsung external samsung samsung
    observe 10000 active p-samsung known samsung external samsung samsung
    local failed_id=$SM_FINALIZATION_ID
    admit_finalizer 10001 || return 1
    sm_finalization_done 11000 1 || return 1
    observe 11001 active p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_FAILED ]] || return 1

    observe 12000 active-aoc p-samsung known aoc external aoc aoc
    observe 22000 active-aoc p-samsung known aoc external aoc aoc
    [[ $SM_PHASE == FINALIZE_PENDING ]] || return 1
    # Cancel the undispatched AOC transaction with changed evidence, then
    # verify Samsung again under the same physical topology.
    observe 23000 active-samsung-2 p-samsung known samsung external samsung samsung
    observe 33000 active-samsung-2 p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == FINALIZE_PENDING ]] &&
        [[ $SM_FINALIZATION_ID != "$failed_id" ]]
}

test_finalizer_admission_requires_same_transition_key () {
    sm_init celtic
    observe 0 active-a p-samsung known samsung external samsung samsung
    observe 10000 active-a p-samsung known samsung external samsung samsung
    local old=$SM_FINALIZATION_ID
    sm_recover 10001 test-boot -
    observe 10001 active-b p-samsung known samsung external samsung samsung
    [[ $SM_PHASE == VERIFYING ]] &&
        [[ $SM_FINALIZATION_ID == "$old" ]] &&
        ! has_action_prefix 'FINALIZE '
}

test_semantically_invalid_numeric_state_is_rejected () {
    local tmp
    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN
    sm_init celtic
    SM_BACKOFF_INDEX=-999
    ! sm_save_state "$tmp"
}

test_recovery_matrix_preserves_progress_without_actions () {
    local phase expected
    while read -r phase expected; do
        sm_init celtic
        SM_PHASE=$phase
        SM_PHYSICAL_TOKEN=p
        SM_AGGRESSIVE_DEADLINE_MS=30000
        SM_NEXT_TIMER_MS=40000
        case $phase in
            APPLY_FAILED)
                SM_APPLICATION_STATUS=failed
                SM_APPLICATION_EXIT_STATUS=1
                SM_PENDING_APPLICATION_KEY='1|samsung|ready'
                SM_PENDING_APPLICATION_PROFILE=samsung
                SM_PENDING_APPLICATION_SCOPE=external
                ;;
            FINALIZE_FAILED)
                SM_FINALIZATION_STATUS=failed
                SM_FINALIZATION_PROFILE=samsung
                SM_FINALIZATION_ID=e1
                ;;
        esac
        sm_recover 1000 test-boot - || return 1
        [[ $SM_PHASE == "$expected" ]] || return 1
        ! has_action_prefix 'APPLY ' || return 1
        ! has_action_prefix 'FINALIZE ' || return 1
    done <<'EOF'
DISCOVER_FAST DISCOVER_FAST
WAIT_SLOW WAIT_SLOW
UNSUPPORTED UNSUPPORTED
APPLY_FAILED APPLY_FAILED
FINALIZE_FAILED FINALIZE_FAILED
EOF
}

test_boot_mismatch_discards_monotonic_wait () {
    sm_init celtic old-boot
    SM_PHASE=WAIT_SLOW
    SM_NEXT_TIMER_MS=999999
    sm_recover 100 new-boot -
    [[ $SM_BOOT_ID == new-boot ]] && [[ $SM_PHASE == RECOVERING ]] &&
        has_action 'SCHEDULE 0'
}

run_test 'laptop startup adopts current profile as baseline' test_laptop_startup_adopts_baseline
run_test 'genuine external plug applies and finalizes once' test_genuine_external_plug_applies_and_finalizes_once
run_test 'readiness after fast deadline progresses without DRM event' test_readiness_after_fast_deadline_needs_no_event
run_test 'EDID loss retains external candidate without fallback' test_edid_loss_retains_candidate_without_fallback
run_test 'transient internal detection never applies with external present' test_transient_internal_detection_never_applies_with_external
run_test 'unchanged application evidence is deduplicated' test_unchanged_application_evidence_is_deduplicated
run_test 'event before application dispatch re-emits admission' test_event_between_application_admission_and_dispatch_reemits
run_test 'event before finalizer dispatch re-emits admission' test_event_between_finalization_admission_and_dispatch_reemits
run_test 'interrupted verification restarts full proof window' test_interrupted_verification_restarts_full_window
run_test 'unplug requires two samples spanning one second' test_unplug_requires_two_samples_spanning_one_second
run_test 'DRM event resets unplug proof' test_drm_event_resets_unplug_proof
run_test 'resume to same profile skips finalization' test_resume_to_same_profile_skips_finalization
run_test 'automatic X transition finalizes without apply' test_automatic_x_transition_finalizes_without_apply
run_test 'connector rename keeps profile identity' test_connector_rename_keeps_profile_identity
run_test 'unknown external becomes terminal but health checked' test_unknown_external_becomes_terminal_but_health_checked
run_test 'changed evidence restarts fast discovery after slow wait' test_changed_evidence_restarts_fast_discovery_after_slow_wait
run_test 'fast timer stops at aggressive deadline' test_fast_timer_does_not_cross_aggressive_deadline
run_test 'overdue fast recovery fires immediately' test_overdue_fast_recovery_fires_immediately
run_test 'restart from apply admission requires fresh observation' test_restart_from_apply_pending_requires_fresh_observation
run_test 'restart after dispatch does not repeat apply' test_restart_from_dispatched_application_does_not_repeat
run_test 'restart from verifying resets proof' test_restart_from_verifying_resets_proof
run_test 'restart from finalizer admission requires fresh observation' test_restart_from_finalize_pending_requires_fresh_observation
run_test 'restart during finalizer reattaches' test_restart_during_finalizer_reattaches
run_test 'application failure is explicit terminal state' test_application_failure_is_terminal_not_silently_deduplicated
run_test 'finalizer success requires fresh valid observation' test_finalizer_success_requires_fresh_valid_observation
run_test 'topology change stops running finalizer' test_topology_change_stops_running_finalizer
run_test 'failed transition allows different profile on same topology' test_failed_transition_allows_different_profile_same_topology
run_test 'persistence round-trips multiple application keys' test_persistence_round_trip_multiple_application_keys
run_test 'persistence rejects truncation without mutation' test_persistence_rejects_truncation_without_partial_mutation
run_test 'persistence rejects duplicate and arithmetic payload' test_persistence_rejects_duplicate_and_arithmetic_payload
run_test 'new physical epoch gets new finalization ID' test_new_epoch_gets_new_finalization_id
run_test 'invalid changed token preserves running finalizer' test_invalid_changed_token_preserves_running_finalizer
run_test 'invalid changed token preserves completed tombstone' test_invalid_changed_token_preserves_completed_tombstone
run_test 'invalid changed token preserves application dedup' test_invalid_changed_token_preserves_application_dedup
run_test 'failed away-back transition allocates new finalization ID' test_failed_away_back_allocates_new_finalization_id
run_test 'finalizer admission requires same transition key' test_finalizer_admission_requires_same_transition_key
run_test 'semantic numeric corruption is rejected' test_semantically_invalid_numeric_state_is_rejected
run_test 'recovery matrix preserves progress without stale actions' test_recovery_matrix_preserves_progress_without_actions
run_test 'boot mismatch discards monotonic wait' test_boot_mismatch_discards_monotonic_wait

if [[ $failures -ne 0 ]]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi
