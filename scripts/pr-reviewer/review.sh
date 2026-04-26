#!/usr/bin/env bash

set -euo pipefail

WORKSPACE="${AGENT_WORKSPACE:-$PWD}"
MODEL="${CURSOR_MODEL:-gpt-5.3-codex-xhigh}"
AGENT_MODE="${AGENT_MODE:-review}"
TARGET_REPO_PATH="${TARGET_REPO_PATH:-$WORKSPACE}"
CONTEXT_DIR="${CONTEXT_DIR:-$WORKSPACE/context}"
PROMPT_PATH="${PROMPT_PATH:-}"
CONCERNS_OUT="${CONCERNS_OUT:-$CONTEXT_DIR/concerns.json}"
SOURCE_PR_REPO="${SOURCE_PR_REPO:-unknown-repo}"
SOURCE_PR_NUMBER="${SOURCE_PR_NUMBER:-unknown-pr}"
SOURCE_PR_URL="${SOURCE_PR_URL:-unknown-url}"
SOURCE_PR_HEAD_REF="${SOURCE_PR_HEAD_REF:-unknown-head}"
SOURCE_PR_HEAD_SHA="${SOURCE_PR_HEAD_SHA:-unknown-sha}"
SOURCE_PR_BASE_REF="${SOURCE_PR_BASE_REF:-main}"
RAW_STREAM_LOG="${RAW_STREAM_LOG:-/tmp/cr-killer-${AGENT_MODE}-${SOURCE_PR_NUMBER}.jsonl}"

if [[ -z "${CURSOR_API_KEY:-}" ]]; then
  echo "Error: CURSOR_API_KEY is not set." >&2
  exit 1
fi

if ! command -v agent >/dev/null 2>&1; then
  echo "Error: 'agent' CLI not found in PATH." >&2
  exit 1
fi

if [[ ! -d "${TARGET_REPO_PATH}" ]]; then
  echo "Error: target repo path does not exist: ${TARGET_REPO_PATH}" >&2
  exit 1
fi

if [[ ! -d "${CONTEXT_DIR}" ]]; then
  echo "Error: context dir does not exist: ${CONTEXT_DIR}" >&2
  exit 1
fi

if [[ -z "${PROMPT_PATH}" || ! -f "${PROMPT_PATH}" ]]; then
  echo "Error: PROMPT_PATH is not set or file missing: ${PROMPT_PATH}" >&2
  exit 1
fi

PROMPT_TEMPLATE="$(cat "${PROMPT_PATH}")"

HEADER="$(cat <<EOF
Runtime variables (substitute everywhere you see them below):
- SOURCE_PR_REPO = ${SOURCE_PR_REPO}
- SOURCE_PR_NUMBER = ${SOURCE_PR_NUMBER}
- SOURCE_PR_URL = ${SOURCE_PR_URL}
- SOURCE_PR_HEAD_REF = ${SOURCE_PR_HEAD_REF}
- SOURCE_PR_HEAD_SHA = ${SOURCE_PR_HEAD_SHA}
- SOURCE_PR_BASE_REF = ${SOURCE_PR_BASE_REF}
- TARGET_REPO_PATH = ${TARGET_REPO_PATH}
- CONTEXT_DIR = ${CONTEXT_DIR}
- CONCERNS_OUT = ${CONCERNS_OUT}
- AGENT_MODE = ${AGENT_MODE}
EOF
)"

QUERY="${HEADER}

---

${PROMPT_TEMPLATE}"

echo "===== CR-Killer agent run ====="
echo "Mode:        ${AGENT_MODE}"
echo "PR:          ${SOURCE_PR_REPO}#${SOURCE_PR_NUMBER}"
echo "Head:        ${SOURCE_PR_HEAD_REF}@${SOURCE_PR_HEAD_SHA}"
echo "Base:        ${SOURCE_PR_BASE_REF}"
echo "Workspace:   ${WORKSPACE}"
echo "Target path: ${TARGET_REPO_PATH}"
echo "Context:     ${CONTEXT_DIR}"
echo "Prompt:      ${PROMPT_PATH}"
echo "Output:      ${CONCERNS_OUT}"
echo "Model:       ${MODEL}"
echo "Raw stream:  ${RAW_STREAM_LOG}"
echo "================================"

rm -f "${CONCERNS_OUT}"

run_agent() {
  agent --print \
    -p "${QUERY}" \
    --model "${MODEL}" \
    --workspace "${WORKSPACE}" \
    --output-format stream-json \
    --trust
}

if command -v jq >/dev/null 2>&1; then
  echo "Readable stream enabled. Raw stream log: ${RAW_STREAM_LOG}"
  run_agent | tee "${RAW_STREAM_LOG}" | jq --unbuffered -r '
    def txt:
      if . == null then ""
      elif type == "string" then .
      else tostring
      end;
    def compact:
      txt | gsub("[\\r\\n]+"; " ");
    def trunc(n):
      if (length > n) then (.[:n] + "...") else . end;
    def content_text:
      if . == null then ""
      elif type == "string" then .
      elif type == "array" then
        [ .[] |
          if . == null then ""
          elif type == "string" then .
          elif type == "object" then (.text // .content // .delta // .value // "")
          else ""
          end
        ] | join("")
      elif type == "object" then
        (.text // .content // .delta // .value // "")
      else ""
      end;
    def extract_command:
      if . == null then ""
      elif type == "object" then (.command // "")
      elif type == "string" then (try (fromjson | .command) catch "")
      else ""
      end;
    if .type == "system" then
      "SYSTEM     | session=" + (.session_id // "-")
    elif .type == "assistant" then
      ((.message.content // .content // .text // .delta // empty) | content_text | compact) as $msg
      | if ($msg | length) > 0 then "ASSISTANT  | " + $msg else empty end
    elif .type == "tool_call" then
      (.tool_call // .call // .toolCall // {}) as $c
      | (($c.args // .args // empty) | extract_command | compact) as $cmd
      | (($c.name // $c.tool_name // $c.tool // $c.call_type // $c.value // .tool_name // .name // .tool // .call.name // .call.tool_name // .tool_call.name // .tool_call.tool_name // .tool_call.call_type // .tool_call.value // "") | txt) as $tool_raw
      | (if ($tool_raw | length) > 0 then $tool_raw elif ($cmd | length) > 0 then "shell" else "unknown-tool" end) as $tool
      | ($tool | trunc(60)) as $tool_short
      | (if ($cmd | length) > 0 and ($tool == "shell" or $tool == "unknown-tool") then " :: " + ($cmd | trunc(140)) else "" end) as $extra
      | if .subtype == "started" then
          "TOOL START | " + $tool_short + $extra
        elif .subtype == "completed" then
          "TOOL DONE  | " + $tool_short
        else
          "TOOL EVENT | " + $tool_short + " (" + (.subtype // "-") + ")"
        end
    elif .type == "result" then
      "RESULT     | " + (.status // "completed")
    elif .type == "error" then
      "ERROR      | " + ((.message // .error // .details // "unknown error") | compact)
    else
      empty
    end
  '
else
  echo "jq not found; falling back to raw stream-json output."
  run_agent
fi

if [[ "${AGENT_MODE}" == "review" ]]; then
  if [[ ! -s "${CONCERNS_OUT}" ]]; then
    echo "Warning: agent did not produce concerns.json at ${CONCERNS_OUT}; writing empty stub so post step can still run reconciliation." >&2
    echo '{"pr_summary":"Agent produced no output.","concerns":[]}' > "${CONCERNS_OUT}"
  fi

  if ! jq -e '.concerns | type == "array"' "${CONCERNS_OUT}" >/dev/null 2>&1; then
    echo "Error: concerns.json at ${CONCERNS_OUT} is not valid JSON with a .concerns array." >&2
    echo "--- file contents ---" >&2
    cat "${CONCERNS_OUT}" >&2 || true
    exit 2
  fi

  count="$(jq '.concerns | length' "${CONCERNS_OUT}")"
  echo "Agent produced ${count} concern(s)."
fi
