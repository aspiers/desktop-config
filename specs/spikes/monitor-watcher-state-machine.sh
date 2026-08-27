#!/bin/bash

# Pure, non-deployed reducer spike for monitor-watcher-state-machine-v2.md.
# It executes no display commands. Callers inject monotonic time and canonical
# observations, then explicitly acknowledge action dispatch.

SM_SCHEMA_VERSION=3
SM_AGGRESSIVE_BUDGET_MS=${SM_AGGRESSIVE_BUDGET_MS:-30000}
SM_PROFILE_STABILITY_MS=${SM_PROFILE_STABILITY_MS:-10000}
SM_EVENT_QUIET_MS=${SM_EVENT_QUIET_MS:-5000}
SM_UNKNOWN_STABILITY_MS=${SM_UNKNOWN_STABILITY_MS:-10000}
SM_UNPLUG_STABILITY_MS=${SM_UNPLUG_STABILITY_MS:-1000}
SM_HEALTH_POLL_MS=${SM_HEALTH_POLL_MS:-60000}
SM_FAST_DELAYS_MS=(0 250 500 1000 2000)
SM_SLOW_DELAYS_MS=(5000 10000 20000 30000)

SM_STATE_KEYS=(
    SM_BOOT_ID SM_PHASE SM_PHYSICAL_EPOCH SM_PHYSICAL_TOKEN SM_RECONCILE_EPOCH
    SM_CANDIDATE_PROFILE SM_CANDIDATE_SCOPE SM_CANDIDATE_OBSERVATION_KEY
    SM_AGGRESSIVE_DEADLINE_MS SM_NEXT_TIMER_MS SM_BACKOFF_INDEX
    SM_VERIFY_SINCE_MS SM_VERIFY_KEY SM_LAST_DRM_AT_MS
    SM_ATTEMPTED_PROBE_KEYS SM_PENDING_PROBE_KEY SM_PENDING_PROBE_PROFILE
    SM_PENDING_PROBE_OUTPUT SM_PENDING_PROBE_INTERNAL_OUTPUT SM_PENDING_PROBE_MODE
    SM_PROBE_STATUS SM_PROBE_EXIT_STATUS
    SM_ATTEMPTED_APPLICATION_KEYS SM_PENDING_APPLICATION_KEY
    SM_PENDING_APPLICATION_PROFILE SM_PENDING_APPLICATION_SCOPE
    SM_APPLICATION_STATUS SM_APPLICATION_EXIT_STATUS SM_STABLE_PROFILE
    SM_DESKTOP_FINALIZED_PROFILE SM_FINALIZATION_SEQUENCE SM_FINALIZATION_ID
    SM_FINALIZATION_PROFILE SM_FINALIZATION_TRANSITION_KEY
    SM_FINALIZATION_STATUS SM_FINALIZATION_EXIT_STATUS
    SM_UNKNOWN_KEY SM_UNKNOWN_SINCE_MS SM_UNPLUG_SINCE_MS
    SM_UNPLUG_SAMPLES SM_EXTERNAL_INTENT SM_BASELINE_ADOPTION
    SM_LAST_OBSERVATION_KEY SM_LAST_EXTERNAL_STATE
)

sm_reset_actions() {
    SM_ACTIONS=()
}

sm_emit() {
    SM_ACTIONS+=("$*")
    SM_ACTION_JOURNAL+=("$*")
}

sm_schedule() {
    local now_ms=$1
    local delay_ms=$2

    ((delay_ms < 0)) && delay_ms=0
    SM_NEXT_TIMER_MS=$((now_ms + delay_ms))
    sm_emit "SCHEDULE $delay_ms"
}

sm_init() {
    local finalized_profile=${1:--}
    local boot_id=${2:-test-boot}

    SM_BOOT_ID=$boot_id
    SM_PHASE=RECOVERING
    SM_PHYSICAL_EPOCH=0
    SM_PHYSICAL_TOKEN=-
    SM_RECONCILE_EPOCH=0
    SM_CANDIDATE_PROFILE=-
    SM_CANDIDATE_SCOPE=-
    SM_CANDIDATE_OBSERVATION_KEY=-
    SM_AGGRESSIVE_DEADLINE_MS=0
    SM_NEXT_TIMER_MS=0
    SM_BACKOFF_INDEX=0
    SM_VERIFY_SINCE_MS=-1
    SM_VERIFY_KEY=-
    SM_LAST_DRM_AT_MS=-1
    SM_ATTEMPTED_PROBE_KEYS= # gitleaks:allow
    SM_PENDING_PROBE_KEY=-
    SM_PENDING_PROBE_PROFILE=-
    SM_PENDING_PROBE_OUTPUT=-
    SM_PENDING_PROBE_INTERNAL_OUTPUT=-
    SM_PENDING_PROBE_MODE=-
    SM_PROBE_STATUS=-
    SM_PROBE_EXIT_STATUS=-
    SM_ATTEMPTED_APPLICATION_KEYS= # gitleaks:allow
    SM_PENDING_APPLICATION_KEY=-
    SM_PENDING_APPLICATION_PROFILE=-
    SM_PENDING_APPLICATION_SCOPE=-
    SM_APPLICATION_STATUS=-
    SM_APPLICATION_EXIT_STATUS=-
    SM_STABLE_PROFILE=-
    SM_DESKTOP_FINALIZED_PROFILE=$finalized_profile
    SM_FINALIZATION_SEQUENCE=0
    SM_FINALIZATION_ID=-
    SM_FINALIZATION_PROFILE=-
    SM_FINALIZATION_TRANSITION_KEY=-
    SM_FINALIZATION_STATUS=-
    SM_FINALIZATION_EXIT_STATUS=-
    SM_UNKNOWN_KEY=-
    SM_UNKNOWN_SINCE_MS=-1
    SM_UNPLUG_SINCE_MS=-1
    SM_UNPLUG_SAMPLES=0
    SM_EXTERNAL_INTENT=0
    SM_BASELINE_ADOPTION=0
    [[ $finalized_profile == - ]] && SM_BASELINE_ADOPTION=1
    SM_LAST_OBSERVATION_KEY=-
    SM_LAST_EXTERNAL_STATE=-
    SM_ACTIONS=()
    SM_ACTION_JOURNAL=()
}

sm_invalidate_verification() {
    SM_VERIFY_SINCE_MS=-1
    SM_VERIFY_KEY=-
}

sm_reset_unplug_proof() {
    SM_UNPLUG_SINCE_MS=-1
    SM_UNPLUG_SAMPLES=0
}

sm_new_physical_epoch() {
    local now_ms=$1
    local physical_token=$2
    local retain_external_intent=${3:-0}

    SM_PHYSICAL_EPOCH=$((SM_PHYSICAL_EPOCH + 1))
    SM_RECONCILE_EPOCH=$((SM_RECONCILE_EPOCH + 1))
    SM_PHYSICAL_TOKEN=$physical_token
    SM_CANDIDATE_PROFILE=-
    SM_CANDIDATE_SCOPE=-
    SM_CANDIDATE_OBSERVATION_KEY=-
    SM_AGGRESSIVE_DEADLINE_MS=$((now_ms + SM_AGGRESSIVE_BUDGET_MS))
    SM_BACKOFF_INDEX=0
    sm_invalidate_verification
    SM_ATTEMPTED_PROBE_KEYS= # gitleaks:allow
    SM_PENDING_PROBE_KEY=-
    SM_PENDING_PROBE_PROFILE=-
    SM_PENDING_PROBE_OUTPUT=-
    SM_PENDING_PROBE_INTERNAL_OUTPUT=-
    SM_PENDING_PROBE_MODE=-
    SM_PROBE_STATUS=-
    SM_PROBE_EXIT_STATUS=-
    SM_ATTEMPTED_APPLICATION_KEYS= # gitleaks:allow
    SM_PENDING_APPLICATION_KEY=-
    SM_PENDING_APPLICATION_PROFILE=-
    SM_PENDING_APPLICATION_SCOPE=-
    SM_APPLICATION_STATUS=-
    SM_APPLICATION_EXIT_STATUS=-
    SM_FINALIZATION_ID=-
    SM_FINALIZATION_PROFILE=-
    SM_FINALIZATION_TRANSITION_KEY=-
    SM_FINALIZATION_STATUS=-
    SM_FINALIZATION_EXIT_STATUS=-
    SM_UNKNOWN_KEY=-
    SM_UNKNOWN_SINCE_MS=-1
    sm_reset_unplug_proof
    SM_EXTERNAL_INTENT=$retain_external_intent
    SM_BASELINE_ADOPTION=0
}

sm_probe_key_seen() {
    local wanted=$1

    [[ $SM_ATTEMPTED_PROBE_KEYS == *";$wanted;"* ]]
}

sm_remember_probe_key() {
    local key=$1

    SM_ATTEMPTED_PROBE_KEYS+=";$key;"
}

sm_application_key_seen() {
    local wanted=$1

    [[ $SM_ATTEMPTED_APPLICATION_KEYS == *";$wanted;"* ]]
}

sm_remember_application_key() {
    local key=$1

    SM_ATTEMPTED_APPLICATION_KEYS+=";$key;"
}

sm_next_fast_delay() {
    local now_ms=$1
    local index=$SM_BACKOFF_INDEX
    local last=$((${#SM_FAST_DELAYS_MS[@]} - 1))
    local remaining=$((SM_AGGRESSIVE_DEADLINE_MS - now_ms))

    ((index > last)) && index=$last
    REPLY=${SM_FAST_DELAYS_MS[$index]}
    ((REPLY > remaining)) && REPLY=$remaining
    ((REPLY < 0)) && REPLY=0
    ((SM_BACKOFF_INDEX < last)) && SM_BACKOFF_INDEX=$((SM_BACKOFF_INDEX + 1))
}

sm_next_slow_delay() {
    local index=$SM_BACKOFF_INDEX
    local last=$((${#SM_SLOW_DELAYS_MS[@]} - 1))

    ((index > last)) && index=$last
    REPLY=${SM_SLOW_DELAYS_MS[$index]}
    ((SM_BACKOFF_INDEX < last)) && SM_BACKOFF_INDEX=$((SM_BACKOFF_INDEX + 1))
}

sm_enter_wait() {
    local now_ms=$1

    sm_invalidate_verification
    if ((now_ms < SM_AGGRESSIVE_DEADLINE_MS)); then
        [[ $SM_PHASE == DISCOVER_FAST ]] || SM_BACKOFF_INDEX=0
        SM_PHASE=DISCOVER_FAST
        sm_next_fast_delay "$now_ms"
    else
        [[ $SM_PHASE == WAIT_SLOW ]] || SM_BACKOFF_INDEX=0
        SM_PHASE=WAIT_SLOW
        sm_next_slow_delay
    fi
    sm_schedule "$now_ms" "$REPLY"
}

sm_begin_verification() {
    local now_ms=$1
    local profile=$2
    local observation_key=$3
    local scope=${4:-external}

    SM_PHASE=VERIFYING
    SM_CANDIDATE_PROFILE=$profile
    SM_CANDIDATE_SCOPE=$scope
    SM_CANDIDATE_OBSERVATION_KEY=$observation_key
    if [[ $SM_VERIFY_KEY != "$observation_key" || $SM_VERIFY_SINCE_MS -lt 0 ]]; then
        SM_VERIFY_SINCE_MS=$now_ms
        SM_VERIFY_KEY=$observation_key
    fi
    sm_schedule "$now_ms" 1000
}

sm_request_probe() {
    local now_ms=$1
    local profile=$2
    local observation_key=$3
    local output=$4
    local internal_output=$5
    local mode=$6
    local probe_key="$SM_PHYSICAL_EPOCH|$profile|$observation_key"

    SM_BASELINE_ADOPTION=0
    if sm_probe_key_seen "$probe_key"; then
        sm_enter_wait "$now_ms"
        return
    fi

    SM_PENDING_PROBE_KEY=$probe_key
    SM_PENDING_PROBE_PROFILE=$profile
    SM_PENDING_PROBE_OUTPUT=$output
    SM_PENDING_PROBE_INTERNAL_OUTPUT=$internal_output
    SM_PENDING_PROBE_MODE=$mode
    SM_PROBE_STATUS=admitted
    SM_PROBE_EXIT_STATUS=-
    SM_PHASE=PROBE_PENDING
    SM_NEXT_TIMER_MS=$now_ms
    sm_emit "PROBE $profile $output $internal_output $mode $probe_key"
}

sm_probe_dispatched() {
    local now_ms=$1

    sm_reset_actions
    [[ $SM_PHASE == PROBE_PENDING && $SM_PENDING_PROBE_KEY != - ]] || return 1
    sm_remember_probe_key "$SM_PENDING_PROBE_KEY"
    SM_PROBE_STATUS=running
    SM_PHASE=PROBING
    SM_NEXT_TIMER_MS=0
    sm_emit "PROBE_DISPATCHED $SM_PENDING_PROBE_PROFILE"
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_probe_done() {
    local now_ms=$1
    local status=${2:-0}

    sm_reset_actions
    [[ $SM_PHASE == PROBING ]] || return 1
    SM_PROBE_EXIT_STATUS=$status
    if [[ $status == 0 ]]; then
        SM_PROBE_STATUS=succeeded
        SM_PENDING_PROBE_KEY=-
        SM_PENDING_PROBE_PROFILE=-
        SM_PENDING_PROBE_OUTPUT=-
        SM_PENDING_PROBE_INTERNAL_OUTPUT=-
        SM_PENDING_PROBE_MODE=-
        SM_PHASE=DISCOVER_FAST
        sm_emit "PROBE_DONE 0"
        sm_schedule "$now_ms" 0
    else
        SM_PROBE_STATUS=failed
        SM_PHASE=PROBE_FAILED
        sm_emit "PROBE_FAILED $SM_PENDING_PROBE_PROFILE $status"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    fi
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_request_application() {
    local now_ms=$1
    local profile=$2
    local scope=$3
    local observation_key=$4
    local application_key="$SM_PHYSICAL_EPOCH|$profile|$observation_key"

    SM_CANDIDATE_PROFILE=$profile
    SM_CANDIDATE_SCOPE=$scope
    SM_CANDIDATE_OBSERVATION_KEY=$observation_key
    SM_BASELINE_ADOPTION=0
    if sm_application_key_seen "$application_key"; then
        sm_enter_wait "$now_ms"
        return
    fi

    SM_PENDING_APPLICATION_KEY=$application_key
    SM_PENDING_APPLICATION_PROFILE=$profile
    SM_PENDING_APPLICATION_SCOPE=$scope
    SM_APPLICATION_STATUS=admitted
    SM_APPLICATION_EXIT_STATUS=-
    SM_PHASE=APPLY_PENDING
    SM_NEXT_TIMER_MS=$now_ms
    sm_emit "APPLY $profile $application_key"
}

sm_application_dispatched() {
    local now_ms=$1

    sm_reset_actions
    [[ $SM_PHASE == APPLY_PENDING && $SM_PENDING_APPLICATION_KEY != - ]] || return 1
    sm_remember_application_key "$SM_PENDING_APPLICATION_KEY"
    SM_APPLICATION_STATUS=running
    SM_PHASE=APPLYING
    SM_NEXT_TIMER_MS=0
    sm_emit "APPLICATION_DISPATCHED $SM_PENDING_APPLICATION_PROFILE"
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_application_done() {
    local now_ms=$1
    local status=${2:-0}

    sm_reset_actions
    [[ $SM_PHASE == APPLYING ]] || return 1
    SM_APPLICATION_EXIT_STATUS=$status
    if [[ $status == 0 ]]; then
        SM_APPLICATION_STATUS=succeeded
        SM_PENDING_APPLICATION_KEY=-
        SM_PENDING_APPLICATION_PROFILE=-
        SM_PENDING_APPLICATION_SCOPE=-
        SM_PHASE=DISCOVER_FAST
        sm_invalidate_verification
        sm_emit "APPLICATION_DONE 0"
        sm_schedule "$now_ms" 0
    else
        SM_APPLICATION_STATUS=failed
        SM_PHASE=APPLY_FAILED
        sm_emit "APPLICATION_FAILED $SM_PENDING_APPLICATION_PROFILE $status"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    fi
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_request_finalization() {
    local now_ms=$1
    local profile=$2
    local transition_key="$SM_PHYSICAL_EPOCH|$profile|$SM_CANDIDATE_OBSERVATION_KEY"

    # This function is reached only after a new continuous verification. A
    # pending admission is re-emitted elsewhere, so every call allocates a
    # never-reused sequence number even for away-and-back transitions.
    SM_FINALIZATION_SEQUENCE=$((SM_FINALIZATION_SEQUENCE + 1))
    SM_FINALIZATION_ID="e${SM_BOOT_ID}-${SM_PHYSICAL_EPOCH}-${SM_FINALIZATION_SEQUENCE}-${profile}-${SM_CANDIDATE_OBSERVATION_KEY}"
    SM_FINALIZATION_PROFILE=$profile
    SM_FINALIZATION_TRANSITION_KEY=$transition_key
    SM_FINALIZATION_STATUS=admitted
    SM_FINALIZATION_EXIT_STATUS=-
    SM_PHASE=FINALIZE_PENDING
    SM_NEXT_TIMER_MS=$now_ms
    sm_emit "FINALIZE $SM_FINALIZATION_ID $profile"
}

sm_finalization_dispatched() {
    local now_ms=$1

    sm_reset_actions
    [[ $SM_PHASE == FINALIZE_PENDING && $SM_FINALIZATION_STATUS == admitted ]] || return 1
    SM_FINALIZATION_STATUS=running
    SM_PHASE=FINALIZING
    sm_emit "FINALIZATION_DISPATCHED $SM_FINALIZATION_ID"
    sm_schedule "$now_ms" 1000
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_finalization_done() {
    local now_ms=$1
    local status=$2

    sm_reset_actions
    [[ $SM_PHASE == FINALIZING && $SM_FINALIZATION_STATUS == running ]] || return 1
    SM_FINALIZATION_EXIT_STATUS=$status
    SM_FINALIZATION_STATUS=result_pending
    sm_emit "FINALIZATION_RESULT $SM_FINALIZATION_ID $status"
    sm_schedule "$now_ms" 0
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_commit_finalization_result() {
    local now_ms=$1

    if [[ $SM_FINALIZATION_EXIT_STATUS == 0 ]]; then
        SM_FINALIZATION_STATUS=succeeded
        SM_DESKTOP_FINALIZED_PROFILE=$SM_FINALIZATION_PROFILE
        SM_STABLE_PROFILE=$SM_FINALIZATION_PROFILE
        SM_PHASE=QUIESCENT
        sm_emit "FINALIZATION_DONE $SM_FINALIZATION_ID"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    else
        SM_FINALIZATION_STATUS=failed
        SM_PHASE=FINALIZE_FAILED
        sm_emit "FINALIZATION_FAILED $SM_FINALIZATION_ID $SM_FINALIZATION_EXIT_STATUS"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    fi
}

sm_finish_verification() {
    local now_ms=$1
    local profile=$2

    SM_STABLE_PROFILE=$profile
    if [[ $SM_DESKTOP_FINALIZED_PROFILE == "$profile" ]]; then
        SM_PHASE=QUIESCENT
        sm_emit "STABLE $profile"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    elif ((SM_BASELINE_ADOPTION == 1)) && [[ $SM_DESKTOP_FINALIZED_PROFILE == - ]]; then
        SM_DESKTOP_FINALIZED_PROFILE=$profile
        SM_BASELINE_ADOPTION=0
        SM_PHASE=QUIESCENT
        sm_emit "ADOPT_BASELINE $profile"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
    else
        sm_request_finalization "$now_ms" "$profile"
    fi
}

sm_cancel_finalization() {
    local stop_running=${1:-1}

    if [[ $SM_FINALIZATION_STATUS == running ]]; then
        ((stop_running == 1)) && sm_emit "STOP_FINALIZER $SM_FINALIZATION_ID"
    fi
    SM_FINALIZATION_STATUS=cancelled
    SM_FINALIZATION_EXIT_STATUS=-
    SM_PHASE=DISCOVER_FAST
}

sm_observe() {
    local now_ms=$1
    local observation_key=$2
    local physical_token=$3
    local external_state=$4
    local target_profile=$5
    local target_scope=$6
    local exact_profile=$7
    local current_profile=$8
    local valid=${9:-1}
    local identity_profile=${10:--}
    local probe_output=${11:--}
    local probe_internal_output=${12:--}
    local probe_mode=${13:--}
    local initial_phase=$SM_PHASE
    local old_external_intent=$SM_EXTERNAL_INTENT
    local physical_changed=0
    local proof_age quiet_age remaining transition_key

    sm_reset_actions

    if [[ $valid != 1 ]]; then
        # Invalid/torn samples cannot prove a physical transition. Preserve all
        # admitted, running, completed, and failed action evidence; only reset
        # continuous proofs and arrange a prompt clean observation.
        sm_invalidate_verification
        sm_reset_unplug_proof
        case $SM_PHASE in
        FINALIZING | FINALIZE_PENDING | APPLYING | APPLY_PENDING | APPLY_FAILED | \
            PROBING | PROBE_PENDING | PROBE_FAILED | FINALIZE_FAILED) ;;
        *) SM_PHASE=DISCOVER_FAST ;;
        esac
        sm_schedule "$now_ms" 1000
        sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
        return
    fi

    if [[ $SM_PHASE == APPLYING && $physical_token != "$SM_PHYSICAL_TOKEN" ]]; then
        # Preserve the acknowledged autorandr worker until its callback. A new
        # topology cannot authorize another display mutation concurrently.
        sm_emit "STOP_APPLICATION $SM_PENDING_APPLICATION_KEY"
        sm_schedule "$now_ms" 1000
        sm_assert_invariants "$now_ms" "$external_state"
        return
    fi

    if [[ $SM_PHASE == PROBING && $physical_token != "$SM_PHYSICAL_TOKEN" ]]; then
        # Do not erase the acknowledged worker or admit a new display action
        # while the old probe can still mutate X. Request idempotent
        # cancellation; its completion callback then schedules re-observation
        # of the new physical topology.
        sm_emit "STOP_PROBE $SM_PENDING_PROBE_KEY"
        sm_schedule "$now_ms" 1000
        sm_assert_invariants "$now_ms" "$external_state"
        return
    fi

    if [[ $physical_token != "$SM_PHYSICAL_TOKEN" ]]; then
        physical_changed=1
        if [[ $SM_PHASE == FINALIZING &&
            ($SM_FINALIZATION_STATUS == running || $SM_FINALIZATION_STATUS == result_pending) ]]; then
            sm_emit "STOP_FINALIZER $SM_FINALIZATION_ID"
        fi
        sm_new_physical_epoch "$now_ms" "$physical_token" \
            "$([[ $external_state == none ]] && printf '%s' "$old_external_intent" || printf 0)"
        if [[ $initial_phase == RECOVERING && $SM_DESKTOP_FINALIZED_PROFILE == - ]]; then
            SM_BASELINE_ADOPTION=1
        fi
        SM_FINALIZATION_STATUS=-
        SM_FINALIZATION_EXIT_STATUS=-
        SM_PHASE=DISCOVER_FAST
    elif [[ $SM_PHASE == WAIT_SLOW && $observation_key != "$SM_LAST_OBSERVATION_KEY" ]]; then
        # Slow waiting is for unchanged uncertainty. New evidence earns a new
        # fast discovery burst even when no DRM event accompanied it.
        SM_PHASE=DISCOVER_FAST
        SM_AGGRESSIVE_DEADLINE_MS=$((now_ms + SM_AGGRESSIVE_BUDGET_MS))
        SM_BACKOFF_INDEX=0
    fi

    if [[ $external_state != none ]]; then
        SM_EXTERNAL_INTENT=1
        sm_reset_unplug_proof
    fi

    if [[ $SM_PHASE == APPLYING && $physical_changed -eq 0 ]]; then
        sm_emit "APPLICATION_IN_FLIGHT $SM_PENDING_APPLICATION_KEY"
        sm_schedule "$now_ms" 1000
        sm_assert_invariants "$now_ms" "$external_state"
        return
    fi

    if [[ $SM_PHASE == PROBING && $physical_changed -eq 0 ]]; then
        # A DRM event may request observation while the activation command is
        # still running. Preserve the in-flight admission until its completion
        # callback; only then may fresh evidence authorize another action.
        sm_emit "PROBE_IN_FLIGHT $SM_PENDING_PROBE_KEY"
        sm_schedule "$now_ms" 1000
        sm_assert_invariants "$now_ms" "$external_state"
        return
    fi

    if [[ $SM_PHASE == PROBE_FAILED && $physical_changed -eq 0 ]]; then
        if [[ $identity_profile == "$SM_PENDING_PROBE_PROFILE" &&
            $observation_key == "$SM_LAST_OBSERVATION_KEY" ]]; then
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        fi
        SM_PROBE_STATUS=-
        SM_PROBE_EXIT_STATUS=-
        SM_PENDING_PROBE_KEY=-
        SM_PENDING_PROBE_PROFILE=-
        SM_PENDING_PROBE_OUTPUT=-
        SM_PENDING_PROBE_INTERNAL_OUTPUT=-
        SM_PENDING_PROBE_MODE=-
        SM_PHASE=DISCOVER_FAST
    fi

    if [[ $SM_PHASE == APPLY_FAILED && $physical_changed -eq 0 ]]; then
        if [[ $target_profile == "$SM_PENDING_APPLICATION_PROFILE" &&
            $observation_key == "$SM_CANDIDATE_OBSERVATION_KEY" ]]; then
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        fi
        SM_APPLICATION_STATUS=-
        SM_APPLICATION_EXIT_STATUS=-
        SM_PENDING_APPLICATION_KEY=-
        SM_PENDING_APPLICATION_PROFILE=-
        SM_PENDING_APPLICATION_SCOPE=-
        SM_PHASE=DISCOVER_FAST
    fi

    if [[ $SM_PHASE == FINALIZE_FAILED && $physical_changed -eq 0 ]]; then
        if [[ $exact_profile != - && $exact_profile == "$current_profile" &&
            $exact_profile != "$SM_FINALIZATION_PROFILE" ]]; then
            SM_FINALIZATION_STATUS=-
            SM_FINALIZATION_EXIT_STATUS=-
            sm_begin_verification "$now_ms" "$exact_profile" "$observation_key" "$target_scope"
        else
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
        fi
        SM_LAST_OBSERVATION_KEY=$observation_key
        SM_LAST_EXTERNAL_STATE=$external_state
        sm_assert_invariants "$now_ms" "$external_state"
        return
    fi

    if [[ ($SM_PHASE == FINALIZING || $SM_PHASE == FINALIZE_PENDING) &&
        $physical_changed -eq 0 ]]; then
        transition_key="$SM_PHYSICAL_EPOCH|$SM_FINALIZATION_PROFILE|$observation_key"
        if [[ $SM_PHASE == FINALIZE_PENDING &&
            $transition_key != "$SM_FINALIZATION_TRANSITION_KEY" ]]; then
            # A recovered admission is authorized only by the exact clean
            # evidence which created it. Changed evidence must verify anew.
            sm_cancel_finalization 0
            sm_invalidate_verification
        elif [[ $exact_profile == "$SM_FINALIZATION_PROFILE" &&
            $current_profile == "$SM_FINALIZATION_PROFILE" ]]; then
            if [[ $SM_FINALIZATION_STATUS == result_pending ]]; then
                sm_commit_finalization_result "$now_ms"
            elif [[ $SM_PHASE == FINALIZE_PENDING ]]; then
                sm_emit "FINALIZE $SM_FINALIZATION_ID $SM_FINALIZATION_PROFILE"
                sm_schedule "$now_ms" 0
            else
                sm_schedule "$now_ms" 1000
            fi
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        fi
        if [[ $SM_PHASE != FINALIZING && $SM_PHASE != FINALIZE_PENDING ]]; then
            : # admission was cancelled above; continue normal classification
        elif [[ $SM_FINALIZATION_STATUS == result_pending ]]; then
            # Completion already happened. Uncertainty or contradiction cannot
            # undo side effects; keep the tombstone and require explicit review.
            SM_FINALIZATION_STATUS=failed
            SM_PHASE=FINALIZE_FAILED
            sm_emit "FINALIZATION_UNCONFIRMED $SM_FINALIZATION_ID"
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        else
            sm_cancel_finalization
            sm_invalidate_verification
        fi
    fi

    # A dirty event may clear an admitted probe before dispatch. Re-emit it
    # only while the same clean base identity still authorizes it.
    if [[ $SM_PHASE == PROBE_PENDING && $physical_changed -eq 0 ]]; then
        if [[ $external_state == probeable &&
            $identity_profile == "$SM_PENDING_PROBE_PROFILE" &&
            $probe_output == "$SM_PENDING_PROBE_OUTPUT" &&
            $probe_internal_output == "$SM_PENDING_PROBE_INTERNAL_OUTPUT" &&
            $probe_mode == "$SM_PENDING_PROBE_MODE" &&
            $observation_key == "$SM_LAST_OBSERVATION_KEY" ]]; then
            sm_emit "PROBE $SM_PENDING_PROBE_PROFILE $SM_PENDING_PROBE_OUTPUT $SM_PENDING_PROBE_INTERNAL_OUTPUT $SM_PENDING_PROBE_MODE $SM_PENDING_PROBE_KEY"
            sm_schedule "$now_ms" 0
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        fi
        SM_PENDING_PROBE_KEY=-
        SM_PENDING_PROBE_PROFILE=-
        SM_PENDING_PROBE_OUTPUT=-
        SM_PENDING_PROBE_INTERNAL_OUTPUT=-
        SM_PENDING_PROBE_MODE=-
        SM_PROBE_STATUS=cancelled
        SM_PHASE=DISCOVER_FAST
    fi

    # A dirty event may clear an admitted action before dispatch. Re-emit it
    # only while the same clean evidence still authorizes it.
    if [[ $SM_PHASE == APPLY_PENDING && $physical_changed -eq 0 ]]; then
        if [[ $target_profile == "$SM_PENDING_APPLICATION_PROFILE" &&
            $observation_key == "$SM_CANDIDATE_OBSERVATION_KEY" ]]; then
            sm_emit "APPLY $SM_PENDING_APPLICATION_PROFILE $SM_PENDING_APPLICATION_KEY"
            sm_schedule "$now_ms" 0
            SM_LAST_OBSERVATION_KEY=$observation_key
            SM_LAST_EXTERNAL_STATE=$external_state
            sm_assert_invariants "$now_ms" "$external_state"
            return
        fi
        SM_PENDING_APPLICATION_KEY=-
        SM_PENDING_APPLICATION_PROFILE=-
        SM_PENDING_APPLICATION_SCOPE=-
        SM_APPLICATION_STATUS=cancelled
        SM_PHASE=DISCOVER_FAST
    fi

    # Internal-only autorandr detection is never eligible while an external
    # connector is present, even if the EDID is missing.
    if [[ $external_state != none && $target_scope == internal ]]; then
        target_profile=-
        target_scope=-
    fi

    if [[ $external_state == none ]]; then
        SM_UNKNOWN_KEY=-
        SM_UNKNOWN_SINCE_MS=-1

        if ((SM_EXTERNAL_INTENT == 1)); then
            if ((SM_UNPLUG_SINCE_MS < 0)); then
                SM_UNPLUG_SINCE_MS=$now_ms
                SM_UNPLUG_SAMPLES=1
            else
                SM_UNPLUG_SAMPLES=$((SM_UNPLUG_SAMPLES + 1))
            fi
            if ((SM_UNPLUG_SAMPLES < 2 || now_ms - SM_UNPLUG_SINCE_MS < SM_UNPLUG_STABILITY_MS)); then
                remaining=$((SM_UNPLUG_STABILITY_MS - (now_ms - SM_UNPLUG_SINCE_MS)))
                ((remaining < 1)) && remaining=1
                sm_invalidate_verification
                SM_PHASE=DISCOVER_FAST
                sm_schedule "$now_ms" "$remaining"
                SM_LAST_OBSERVATION_KEY=$observation_key
                SM_LAST_EXTERNAL_STATE=$external_state
                sm_assert_invariants "$now_ms" "$external_state"
                return
            fi
            SM_EXTERNAL_INTENT=0
            sm_reset_unplug_proof
        fi

        if [[ $exact_profile != - && $exact_profile == "$current_profile" ]]; then
            sm_begin_verification "$now_ms" "$exact_profile" "$observation_key" internal
        elif [[ $target_profile != - && $target_scope == internal ]]; then
            sm_invalidate_verification
            sm_request_application "$now_ms" "$target_profile" internal "$observation_key"
        else
            sm_enter_wait "$now_ms"
        fi
    elif [[ $external_state == probeable ]]; then
        sm_invalidate_verification
        SM_UNKNOWN_KEY=-
        SM_UNKNOWN_SINCE_MS=-1
        if [[ $identity_profile != - && $probe_output != - &&
            $probe_internal_output != - && $probe_mode != - ]]; then
            sm_request_probe "$now_ms" "$identity_profile" "$observation_key" \
                "$probe_output" "$probe_internal_output" "$probe_mode"
        else
            sm_enter_wait "$now_ms"
        fi
    elif [[ $external_state == unknown ]]; then
        sm_invalidate_verification
        if [[ $SM_UNKNOWN_KEY != "$observation_key" ]]; then
            SM_UNKNOWN_KEY=$observation_key
            SM_UNKNOWN_SINCE_MS=$now_ms
        fi
        if ((now_ms - SM_UNKNOWN_SINCE_MS >= SM_UNKNOWN_STABILITY_MS)); then
            SM_PHASE=UNSUPPORTED
            sm_emit "UNSUPPORTED $observation_key"
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
        else
            SM_PHASE=DISCOVER_FAST
            sm_schedule "$now_ms" 1000
        fi
    elif [[ $exact_profile != - && $exact_profile == "$current_profile" ]]; then
        SM_UNKNOWN_KEY=-
        SM_UNKNOWN_SINCE_MS=-1
        sm_begin_verification "$now_ms" "$exact_profile" "$observation_key" external
    elif [[ $target_profile != - && $target_scope == external ]]; then
        SM_UNKNOWN_KEY=-
        SM_UNKNOWN_SINCE_MS=-1
        sm_invalidate_verification
        sm_request_application "$now_ms" "$target_profile" external "$observation_key"
    else
        # Missing/truncated EDID is uncertainty. Retain external intent and any
        # candidate, but never apply an internal fallback.
        sm_enter_wait "$now_ms"
    fi

    if [[ $SM_PHASE == VERIFYING ]]; then
        proof_age=$((now_ms - SM_VERIFY_SINCE_MS))
        if ((SM_LAST_DRM_AT_MS < 0)); then
            quiet_age=$SM_EVENT_QUIET_MS
        else
            quiet_age=$((now_ms - SM_LAST_DRM_AT_MS))
        fi
        if ((proof_age >= SM_PROFILE_STABILITY_MS && quiet_age >= SM_EVENT_QUIET_MS)); then
            SM_ACTIONS=()
            sm_finish_verification "$now_ms" "$exact_profile"
        fi
    fi

    SM_LAST_OBSERVATION_KEY=$observation_key
    SM_LAST_EXTERNAL_STATE=$external_state
    sm_assert_invariants "$now_ms" "$external_state"
}

sm_drm_event() {
    local now_ms=$1

    sm_reset_actions
    SM_LAST_DRM_AT_MS=$now_ms
    sm_invalidate_verification
    sm_reset_unplug_proof
    case $SM_PHASE in
    QUIESCENT | VERIFYING | UNSUPPORTED) SM_PHASE=DISCOVER_FAST ;;
    PROBE_PENDING | APPLY_PENDING | FINALIZE_PENDING) ;; # admission remains re-emittable
    esac
    sm_schedule "$now_ms" 0
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_recover() {
    local now_ms=$1
    local current_boot_id=${2:-$SM_BOOT_ID}
    local finalizer_status=${3:--}

    sm_reset_actions
    if [[ $current_boot_id != "$SM_BOOT_ID" ]]; then
        local finalized=$SM_DESKTOP_FINALIZED_PROFILE
        sm_init "$finalized" "$current_boot_id"
        sm_schedule "$now_ms" 0
        return
    fi

    case $SM_PHASE in
    PROBE_PENDING)
        # Re-observe the persisted base-identity admission before dispatch.
        sm_schedule "$now_ms" 0
        ;;
    PROBING)
        # The activation command was dispatched, but its outcome is unknown.
        # Never repeat it under unchanged evidence after a restart.
        SM_PROBE_STATUS=unknown
        SM_PHASE=PROBE_FAILED
        sm_emit "PROBE_UNKNOWN $SM_PENDING_PROBE_PROFILE"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
        ;;
    APPLY_PENDING)
        # A topology change may have happened while the watcher was down.
        # Preserve admission, but require a fresh observation to re-emit it.
        sm_schedule "$now_ms" 0
        ;;
    APPLYING)
        # Dispatch was acknowledged, but completion is indeterminate after
        # restart. Do not repeat side effects under the same evidence.
        SM_APPLICATION_STATUS=unknown
        SM_PHASE=APPLY_FAILED
        sm_emit "APPLICATION_UNKNOWN $SM_PENDING_APPLICATION_PROFILE"
        sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
        ;;
    VERIFYING)
        sm_invalidate_verification
        sm_schedule "$now_ms" 0
        ;;
    DISCOVER_FAST)
        # Overdue work fires now. The subsequent observation chooses the
        # slow backoff; recovery itself never hides an overdue timer.
        sm_schedule "$now_ms" 0
        ;;
    WAIT_SLOW)
        if ((SM_NEXT_TIMER_MS <= now_ms)); then
            sm_schedule "$now_ms" 0
        else
            sm_emit "SCHEDULE $((SM_NEXT_TIMER_MS - now_ms))"
        fi
        ;;
    FINALIZE_PENDING)
        # As with application admission, re-observe before dispatch.
        sm_schedule "$now_ms" 0
        ;;
    FINALIZING)
        case $finalizer_status in
        running)
            SM_FINALIZATION_STATUS=running
            sm_emit "REATTACH_FINALIZER $SM_FINALIZATION_ID"
            sm_schedule "$now_ms" 1000
            ;;
        succeeded)
            SM_FINALIZATION_STATUS=result_pending
            SM_FINALIZATION_EXIT_STATUS=0
            sm_schedule "$now_ms" 0
            ;;
        failed)
            SM_FINALIZATION_STATUS=result_pending
            SM_FINALIZATION_EXIT_STATUS=1
            sm_schedule "$now_ms" 0
            ;;
        *)
            SM_FINALIZATION_STATUS=failed
            SM_PHASE=FINALIZE_FAILED
            sm_emit "FINALIZATION_FAILED $SM_FINALIZATION_ID unknown"
            sm_schedule "$now_ms" "$SM_HEALTH_POLL_MS"
            ;;
        esac
        ;;
    PROBE_FAILED | APPLY_FAILED | UNSUPPORTED | FINALIZE_FAILED | QUIESCENT)
        if ((SM_NEXT_TIMER_MS <= now_ms)); then
            sm_schedule "$now_ms" 0
        else
            sm_emit "SCHEDULE $((SM_NEXT_TIMER_MS - now_ms))"
        fi
        ;;
    RECOVERING | *) sm_schedule "$now_ms" 0 ;;
    esac
    sm_assert_invariants "$now_ms" "$SM_LAST_EXTERNAL_STATE"
}

sm_snapshot() {
    printf 'phase=%s physical_epoch=%s candidate=%s stable=%s finalized=%s last_observation=%s next_timer_ms=%s\n' \
        "$SM_PHASE" "$SM_PHYSICAL_EPOCH" "$SM_CANDIDATE_PROFILE" \
        "$SM_STABLE_PROFILE" "$SM_DESKTOP_FINALIZED_PROFILE" \
        "$SM_LAST_OBSERVATION_KEY" "$SM_NEXT_TIMER_MS"
}

sm_probe_keys_are_unique() {
    local data=${SM_ATTEMPTED_PROBE_KEYS//;/ }
    local key
    local -a keys=()
    local -A seen=()

    read -r -a keys <<<"$data"
    for key in "${keys[@]}"; do
        [[ -z ${seen[$key]+set} ]] || return 1
        seen[$key]=1
    done
}

sm_application_keys_are_unique() {
    local data=${SM_ATTEMPTED_APPLICATION_KEYS//;/ }
    local key
    local -a keys=()
    local -A seen=()

    read -r -a keys <<<"$data"
    for key in "${keys[@]}"; do
        [[ -z ${seen[$key]+set} ]] || return 1
        seen[$key]=1
    done
}

sm_probe_payload_is_consistent() {
    [[ $SM_PENDING_PROBE_KEY != - &&
        $SM_PENDING_PROBE_PROFILE != - &&
        $SM_PENDING_PROBE_OUTPUT != - &&
        $SM_PENDING_PROBE_INTERNAL_OUTPUT != - &&
        $SM_PENDING_PROBE_MODE != - &&
        $SM_LAST_OBSERVATION_KEY != - &&
        $SM_PENDING_PROBE_KEY == "$SM_PHYSICAL_EPOCH|$SM_PENDING_PROBE_PROFILE|$SM_LAST_OBSERVATION_KEY" ]]
}

sm_assert_invariants() {
    local now_ms=${1:-0}
    local external_state=${2:-$SM_LAST_EXTERNAL_STATE}
    local action profile application_key

    sm_probe_keys_are_unique || {
        echo 'invariant failed: duplicate attempted probe key' >&2
        return 1
    }
    sm_application_keys_are_unique || {
        echo 'invariant failed: duplicate attempted application key' >&2
        return 1
    }

    case $SM_PHASE in
    PROBE_PENDING)
        [[ $SM_PROBE_STATUS == admitted ]] &&
            sm_probe_payload_is_consistent &&
            ! sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || {
            echo 'invariant failed: pending probe lacks admission data' >&2
            return 1
        }
        ;;
    PROBING)
        [[ $SM_PROBE_STATUS == running ]] &&
            sm_probe_payload_is_consistent || return 1
        sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || {
            echo 'invariant failed: dispatched probe key was not persisted' >&2
            return 1
        }
        ;;
    PROBE_FAILED)
        [[ $SM_PROBE_STATUS == failed || $SM_PROBE_STATUS == unknown ]] &&
            sm_probe_payload_is_consistent &&
            sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || return 1
        ;;
    APPLY_PENDING)
        [[ $SM_PENDING_APPLICATION_KEY != - &&
            $SM_PENDING_APPLICATION_PROFILE != - &&
            $SM_PENDING_APPLICATION_SCOPE != - &&
            $SM_APPLICATION_STATUS == admitted ]] || {
            echo 'invariant failed: pending application lacks admission data' >&2
            return 1
        }
        ;;
    APPLYING)
        [[ $SM_APPLICATION_STATUS == running ]] || return 1
        sm_application_key_seen "$SM_PENDING_APPLICATION_KEY" || {
            echo 'invariant failed: dispatched application key was not persisted' >&2
            return 1
        }
        ;;
    APPLY_FAILED)
        [[ $SM_APPLICATION_STATUS == failed || $SM_APPLICATION_STATUS == unknown ]] || return 1
        ;;
    FINALIZE_PENDING)
        [[ $SM_FINALIZATION_STATUS == admitted ]] || return 1
        ;;
    FINALIZING)
        [[ $SM_FINALIZATION_STATUS == running || $SM_FINALIZATION_STATUS == result_pending ]] || return 1
        ;;
    esac

    for action in "${SM_ACTIONS[@]}"; do
        if [[ $action == PROBE\ * ]]; then
            local probe_output probe_internal_output probe_mode
            read -r _ profile probe_output probe_internal_output probe_mode application_key <<<"$action"
            [[ $external_state == probeable &&
                $profile == "$SM_PENDING_PROBE_PROFILE" &&
                $probe_output == "$SM_PENDING_PROBE_OUTPUT" &&
                $probe_internal_output == "$SM_PENDING_PROBE_INTERNAL_OUTPUT" &&
                $probe_mode == "$SM_PENDING_PROBE_MODE" ]] || {
                echo "invariant failed: probe admitted without probeable external identity $profile" >&2
                return 1
            }
            sm_probe_key_seen "$application_key" && {
                echo "invariant failed: duplicate probe admitted for $application_key" >&2
                return 1
            }
        elif [[ $action == APPLY\ * ]]; then
            read -r _ profile application_key <<<"$action"
            if [[ $external_state != none &&
                ($profile != "$SM_CANDIDATE_PROFILE" ||
                $SM_CANDIDATE_SCOPE != external ||
                $SM_PENDING_APPLICATION_SCOPE != external) ]]; then
                echo "invariant failed: external topology admitted non-external candidate $profile" >&2
                return 1
            fi
            sm_application_key_seen "$application_key" && {
                echo "invariant failed: duplicate application admitted for $application_key" >&2
                return 1
            }
        fi
    done

    if [[ $external_state != none &&
        ($SM_PHASE == DISCOVER_FAST || $SM_PHASE == WAIT_SLOW ||
        $SM_PHASE == VERIFYING || $SM_PHASE == UNSUPPORTED ||
        $SM_PHASE == PROBE_PENDING) &&
        $SM_NEXT_TIMER_MS -lt $now_ms ]]; then
        echo 'invariant failed: unresolved external topology has no scheduled timer' >&2
        return 1
    fi

}

sm_value_is_safe_opaque() {
    [[ $1 != *[!A-Za-z0-9._,:+/@\|\;-]* ]]
}

sm_validate_loaded_state() {
    local key value
    local -a numeric_keys=(
        SM_PHYSICAL_EPOCH SM_RECONCILE_EPOCH SM_AGGRESSIVE_DEADLINE_MS
        SM_FINALIZATION_SEQUENCE
        SM_NEXT_TIMER_MS SM_BACKOFF_INDEX SM_VERIFY_SINCE_MS SM_LAST_DRM_AT_MS
        SM_UNKNOWN_SINCE_MS SM_UNPLUG_SINCE_MS SM_UNPLUG_SAMPLES
        SM_EXTERNAL_INTENT SM_BASELINE_ADOPTION
    )

    [[ $SM_PHASE =~ ^(RECOVERING|QUIESCENT|DISCOVER_FAST|PROBE_PENDING|PROBING|PROBE_FAILED|APPLY_PENDING|APPLYING|APPLY_FAILED|VERIFYING|WAIT_SLOW|UNSUPPORTED|FINALIZE_PENDING|FINALIZING|FINALIZE_FAILED)$ ]] || return 1
    for key in "${numeric_keys[@]}"; do
        value=${!key}
        [[ $value =~ ^-?[0-9]{1,15}$ ]] || return 1
    done
    ((SM_PHYSICAL_EPOCH >= 0 && SM_RECONCILE_EPOCH >= 0 && \
    SM_AGGRESSIVE_DEADLINE_MS >= 0 && SM_NEXT_TIMER_MS >= 0 && \
    SM_BACKOFF_INDEX >= 0 && SM_BACKOFF_INDEX < ${#SM_FAST_DELAYS_MS[@]} && \
    SM_FINALIZATION_SEQUENCE >= 0 && SM_UNPLUG_SAMPLES >= 0)) || return 1
    ((SM_VERIFY_SINCE_MS >= -1 && SM_LAST_DRM_AT_MS >= -1 && \
    SM_UNKNOWN_SINCE_MS >= -1 && SM_UNPLUG_SINCE_MS >= -1)) || return 1
    for key in "${SM_STATE_KEYS[@]}"; do
        case $key in
        SM_ATTEMPTED_PROBE_KEYS | SM_ATTEMPTED_APPLICATION_KEYS) [[ ${!key} =~ ^([;][-A-Za-z0-9._,:+/@|]+[;])*$ ]] || return 1 ;;
        *) sm_value_is_safe_opaque "${!key}" || return 1 ;;
        esac
    done
    [[ $SM_PROBE_EXIT_STATUS == - || $SM_PROBE_EXIT_STATUS =~ ^[0-9]{1,3}$ ]] || return 1
    [[ $SM_APPLICATION_EXIT_STATUS == - || $SM_APPLICATION_EXIT_STATUS =~ ^[0-9]{1,3}$ ]] || return 1
    [[ $SM_FINALIZATION_EXIT_STATUS == - || $SM_FINALIZATION_EXIT_STATUS =~ ^[0-9]{1,3}$ ]] || return 1
    [[ $SM_PROBE_EXIT_STATUS == - || $SM_PROBE_EXIT_STATUS -le 255 ]] || return 1
    [[ $SM_APPLICATION_EXIT_STATUS == - || $SM_APPLICATION_EXIT_STATUS -le 255 ]] || return 1
    [[ $SM_FINALIZATION_EXIT_STATUS == - || $SM_FINALIZATION_EXIT_STATUS -le 255 ]] || return 1
    sm_probe_keys_are_unique || return 1
    sm_application_keys_are_unique || return 1
    ((SM_EXTERNAL_INTENT == 0 || SM_EXTERNAL_INTENT == 1)) || return 1
    ((SM_BASELINE_ADOPTION == 0 || SM_BASELINE_ADOPTION == 1)) || return 1
    case $SM_PHASE in
    PROBE_PENDING) [[ $SM_PROBE_STATUS == admitted ]] && sm_probe_payload_is_consistent && ! sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || return 1 ;;
    PROBING) [[ $SM_PROBE_STATUS == running ]] && sm_probe_payload_is_consistent && sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || return 1 ;;
    PROBE_FAILED) [[ $SM_PROBE_STATUS == failed || $SM_PROBE_STATUS == unknown ]] && sm_probe_payload_is_consistent && sm_probe_key_seen "$SM_PENDING_PROBE_KEY" || return 1 ;;
    APPLY_PENDING) [[ $SM_PENDING_APPLICATION_KEY != - && $SM_PENDING_APPLICATION_PROFILE != - && $SM_PENDING_APPLICATION_SCOPE != - && $SM_APPLICATION_STATUS == admitted ]] || return 1 ;;
    APPLYING) [[ $SM_APPLICATION_STATUS == running ]] && sm_application_key_seen "$SM_PENDING_APPLICATION_KEY" || return 1 ;;
    APPLY_FAILED) [[ $SM_APPLICATION_STATUS == failed || $SM_APPLICATION_STATUS == unknown ]] || return 1 ;;
    FINALIZE_PENDING) [[ $SM_FINALIZATION_STATUS == admitted ]] || return 1 ;;
    FINALIZING) [[ $SM_FINALIZATION_STATUS == running || $SM_FINALIZATION_STATUS == result_pending ]] || return 1 ;;
    esac
}

sm_save_state() {
    local path=$1
    local tmp name

    sm_validate_loaded_state || return 1
    for name in "${SM_STATE_KEYS[@]}"; do
        sm_value_is_safe_opaque "${!name}" || return 1
    done

    tmp=$(mktemp "${path}.tmp.XXXXXX") || return 1
    chmod 600 "$tmp" || {
        rm -f "$tmp"
        return 1
    }

    {
        printf 'schema_version\t%s\n' "$SM_SCHEMA_VERSION"
        for name in "${SM_STATE_KEYS[@]}"; do
            printf '%s\t%s\n' "$name" "${!name}"
        done
    } >"$tmp" || {
        rm -f "$tmp"
        return 1
    }
    mv -f "$tmp" "$path"
}

sm_load_state() {
    local path=$1
    local key value name
    local schema_seen=0 expected_count=$((${#SM_STATE_KEYS[@]} + 1))
    local -A parsed=() allowed=()

    [[ -r $path ]] || return 1
    for name in "${SM_STATE_KEYS[@]}"; do allowed[$name]=1; done

    while IFS=$'\t' read -r key value; do
        [[ -n $key && -z ${parsed[$key]+set} ]] || return 1
        case $key in
        schema_version)
            [[ $value == "$SM_SCHEMA_VERSION" ]] || return 1
            schema_seen=1
            parsed[$key]=$value
            ;;
        SM_*)
            [[ ${allowed[$key]:-0} == 1 ]] || return 1
            [[ $value != *$'\t'* && $value != *$'\n'* && $value != *$'\r'* ]] || return 1
            parsed[$key]=$value
            ;;
        *) return 1 ;;
        esac
    done <"$path"

    ((schema_seen == 1 && ${#parsed[@]} == expected_count)) || return 1

    # Validate in a subshell so a rejected record cannot partially mutate the
    # live controller. Only copy into live variables after complete validation.
    if ! (
        for name in "${SM_STATE_KEYS[@]}"; do printf -v "$name" '%s' "${parsed[$name]}"; done
        sm_validate_loaded_state
    ); then
        return 1
    fi
    for name in "${SM_STATE_KEYS[@]}"; do printf -v "$name" '%s' "${parsed[$name]}"; done
    sm_validate_loaded_state
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    cat <<'EOF'
This is a pure state-machine spike. Source it from a synthetic trace driver;
it never invokes autorandr, xrandr, systemd, or setup-monitor.
EOF
fi
