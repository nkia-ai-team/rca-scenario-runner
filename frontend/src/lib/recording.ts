import type { RecordingLevel, RecordingStatus, Tone } from "../types";

// 녹화 상태의 화면 표기. 판정은 서버(recording_status.py)가 stage 와 녹화 대기 큐로 한다.
export const RECORDING_PRESENTATION: Record<
  RecordingLevel,
  { label: string; tone: Tone; confirm: string | null }
> = {
  hold: {
    label: "보류(돌리지 말 것)",
    tone: "rose",
    confirm:
      "녹화 대기 큐에서 보류된 시나리오입니다. 사람 결정 전에는 돌리지 않습니다. 그래도 실행할까요?",
  },
  unverified: {
    label: "녹화 검증 안 됨",
    tone: "amber",
    confirm:
      "정상 녹화가 아직 없는 후보입니다. 주입 결과가 검증되지 않았습니다. 그래도 실행할까요?",
  },
  recorded: { label: "녹화 완료", tone: "emerald", confirm: null },
};

export function needsRunConfirm(status: RecordingStatus | null | undefined): boolean {
  return Boolean(status && !status.recommended);
}

export function runConfirmMessage(status: RecordingStatus): string {
  return (
    RECORDING_PRESENTATION[status.level].confirm ??
    "추천하지 않는 시나리오입니다. 그래도 실행할까요?"
  );
}

// 같은 칸 안에서 보류를 맨 뒤로 보낸다(나머지는 원래 순서 유지).
export function holdLast<T extends { recording?: RecordingStatus | null }>(list: T[]): T[] {
  return [
    ...list.filter((s) => s.recording?.level !== "hold"),
    ...list.filter((s) => s.recording?.level === "hold"),
  ];
}
