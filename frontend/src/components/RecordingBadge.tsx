import { RECORDING_PRESENTATION } from "../lib/recording";
import type { RecordingStatus } from "../types";
import { Pill } from "./Pill";

export function RecordingBadge({ status }: { status: RecordingStatus }) {
  const p = RECORDING_PRESENTATION[status.level];
  return (
    <span title={status.reason} data-testid="recording-badge">
      <Pill tone={p.tone}>{p.label}</Pill>
    </span>
  );
}
