#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  record-t300-camera.sh --host HOST [--duration TIME] [--output FILE]
                           [--fps 1..30] [--crf 0..51]

HOST may also be supplied in T300_HOST. TIME uses FFmpeg syntax such as 30m or
01:30:00. The recording is stored on the laptop, not on the printer or its USB
stick. Press q in FFmpeg to stop cleanly when no duration is supplied.

Defaults:
  output directory  $T300_RECORDING_DIR or .cache/camera-recordings
  fps               $T300_RECORDING_FPS or 10
  CRF               $T300_RECORDING_CRF or 28
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 2
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

host="${T300_HOST:-}"
duration=""
output=""
fps="${T300_RECORDING_FPS:-10}"
crf="${T300_RECORDING_CRF:-28}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)
      [[ $# -ge 2 ]] || die "--host requires a value"
      host="$2"
      shift 2
      ;;
    --duration)
      [[ $# -ge 2 ]] || die "--duration requires a value"
      duration="$2"
      shift 2
      ;;
    --output)
      [[ $# -ge 2 ]] || die "--output requires a file"
      output="$2"
      shift 2
      ;;
    --fps)
      [[ $# -ge 2 ]] || die "--fps requires a value"
      fps="$2"
      shift 2
      ;;
    --crf)
      [[ $# -ge 2 ]] || die "--crf requires a value"
      crf="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[[ -n "$host" ]] || die "Supply --host HOST or set T300_HOST"
[[ "$host" =~ ^([A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(:[0-9]{1,5})?$ ]] ||
  die "HOST must be a hostname or IP address with an optional port"
[[ "$fps" =~ ^[0-9]+$ ]] && ((fps >= 1 && fps <= 30)) ||
  die "--fps must be an integer from 1 to 30"
[[ "$crf" =~ ^[0-9]+$ ]] && ((crf >= 0 && crf <= 51)) ||
  die "--crf must be an integer from 0 to 51"

need_command ffmpeg
need_command ffprobe
need_command df
need_command awk

if [[ -z "$output" ]]; then
  output_dir="${T300_RECORDING_DIR:-${repo_root}/.cache/camera-recordings}"
  timestamp="$(date +%Y%m%d-%H%M%S)"
  output="${output_dir}/t300-${timestamp}.mkv"
else
  output_dir="$(dirname "$output")"
fi
mkdir -p "$output_dir"
[[ ! -e "$output" ]] || die "Output already exists: $output"

free_kib="$(df -Pk "$output_dir" | awk 'NR == 2 {print $4}')"
printf 'Recording T300 camera to %s\n' "$output"
printf 'Input: http://%s/webcam/?action=stream\n' "$host"
printf 'Output: H.264, up to %s fps, CRF %s; free space %.1f GiB\n' \
  "$fps" "$crf" "$(awk -v kib="$free_kib" 'BEGIN {print kib / 1048576}')"
if [[ -n "$duration" ]]; then
  printf 'Duration: %s\n' "$duration"
else
  printf 'Press q to stop and finalize the recording.\n'
fi

args=(
  ffmpeg
  -hide_banner
  -loglevel warning
  -rw_timeout 10000000
  -reconnect 1
  -reconnect_at_eof 1
  -reconnect_streamed 1
  -reconnect_on_network_error 1
  -reconnect_on_http_error 4xx,5xx
  -reconnect_delay_max 5
  -thread_queue_size 1024
  -use_wallclock_as_timestamps 1
  -fflags +genpts+discardcorrupt
  -i "http://${host}/webcam/?action=stream"
)

if [[ -n "$duration" ]]; then
  args+=(-t "$duration")
fi

args+=(
  -an
  -vf "fps=${fps}"
  -c:v libx264
  -preset ultrafast
  -crf "$crf"
  -fps_mode vfr
  -pix_fmt yuv420p
  -flush_packets 1
  -f matroska
  "$output"
)

status=0
interrupted=0
trap 'interrupted=1' INT TERM
"${args[@]}" || status=$?
trap - INT TERM

if [[ ! -s "$output" ]]; then
  rm -f "$output"
  die "FFmpeg produced no recording (exit $status)"
fi

ffprobe -v error \
  -select_streams v:0 \
  -show_entries stream=width,height,avg_frame_rate \
  -show_entries format=duration,size \
  -of default=noprint_wrappers=1 \
  "$output"

if ((status != 0 && interrupted == 0)); then
  die "FFmpeg exited with status $status; the partial Matroska file was retained"
fi

if ((interrupted != 0)); then
  printf 'Recording interrupted cleanly; the Matroska file was retained.\n'
fi
