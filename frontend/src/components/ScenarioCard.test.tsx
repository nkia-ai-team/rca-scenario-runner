import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { holdLast } from "../lib/recording";
import type { RecordingStatus, ScenarioView } from "../types";
import { ScenarioCard } from "./ScenarioCard";

afterEach(cleanup);

function scenario(recording: RecordingStatus | null, id = "F99-R"): ScenarioView {
  return {
    id,
    short_id: id,
    domain: "commerce",
    domain_label: "Commerce",
    name: "시험 시나리오",
    description: "설명",
    cause: "원인",
    propagation: "원인 → 결과",
    expected_alarms: ["신호"],
    estimated_duration_sec: 600,
    script_filename: "run-scenario.sh",
    execution: {
      orchestrator: {
        transport: "local",
        location: "trusted-profile-control",
        timeout_sec: 600,
      },
      injection_points: [],
    } as unknown as ScenarioView["execution"],
    warnings: [],
    difficulty: null,
    expected_rca_root_cause: null,
    cause_domain: null,
    propagation_steps: null,
    expected_clusters: null,
    expected_incidents: null,
    stage: recording?.level === "recorded" ? "official" : "candidate",
    recording,
    num: "99",
    code: id,
    tone: "violet",
    tag: "Scenario",
    propagationHops: ["원인", "결과"],
  };
}

const HOLD: RecordingStatus = {
  level: "hold",
  recommended: false,
  reason: "주입이 노드 붕괴로 번짐",
  detail: "주입이 노드 붕괴로 번짐. 사람 결정 뒤 waiting 으로 되돌린다.",
};
const UNVERIFIED: RecordingStatus = {
  level: "unverified",
  recommended: false,
  reason: "정상 녹화가 아직 없어 주입 결과가 검증되지 않았다",
  detail: "새 시나리오 첫 검증",
};
const RECORDED: RecordingStatus = {
  level: "recorded",
  recommended: true,
  reason: "정상 녹화가 1개 이상 있다",
  detail: null,
};

function renderCard(recording: RecordingStatus | null, runDisabled = false) {
  const onRun = vi.fn();
  render(
    <ScenarioCard
      scn={scenario(recording)}
      status="idle"
      runDisabled={runDisabled}
      isSelected={false}
      onRun={onRun}
      onCleanup={vi.fn()}
    />,
  );
  return onRun;
}

const runButton = () => screen.getByRole("button", { name: "실행" });

describe("ScenarioCard 녹화 상태", () => {
  it.each([
    [HOLD, "보류(돌리지 말 것)"],
    [UNVERIFIED, "녹화 검증 안 됨"],
  ])("추천하지 않는 카드는 배지와 사유 한 줄을 보인다 (%#)", (status, label) => {
    renderCard(status);

    expect(screen.getByTestId("recording-badge").textContent).toBe(label);
    expect(screen.getByTestId("recording-reason").textContent).toBe(`사유: ${status.reason}`);
  });

  it("녹화 완료 카드는 배지만 있고 사유 줄과 확인이 없다", () => {
    const onRun = renderCard(RECORDED);

    expect(screen.getByTestId("recording-badge").textContent).toBe("녹화 완료");
    expect(screen.queryByTestId("recording-reason")).toBeNull();

    fireEvent.click(runButton());

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it("녹화 상태가 없는 옛 시나리오는 표시 없이 바로 실행한다", () => {
    const onRun = renderCard(null);

    expect(screen.queryByTestId("recording-badge")).toBeNull();
    fireEvent.click(runButton());

    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it.each([HOLD, UNVERIFIED])("추천하지 않는 카드는 실행 전에 확인을 받고, 확인하면 실행한다 (%#)", (status) => {
    const onRun = renderCard(status);

    fireEvent.click(runButton());

    const dialog = screen.getByRole("alertdialog", { name: "실행 확인" });
    expect(dialog.textContent).toContain("그래도 실행할까요?");
    expect(dialog.textContent).toContain(status.reason);
    expect(onRun).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "그래도 실행" }));

    expect(onRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("확인에서 취소하면 실행하지 않는다", () => {
    const onRun = renderCard(HOLD);

    fireEvent.click(runButton());
    fireEvent.click(screen.getByRole("button", { name: "취소" }));

    expect(onRun).not.toHaveBeenCalled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("다른 실행 중이라 버튼이 막혀 있으면 확인도 열지 않는다", () => {
    const onRun = renderCard(HOLD, true);

    fireEvent.click(runButton());

    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(onRun).not.toHaveBeenCalled();
  });

  it("상세 보기에 보류 사유 전문이 나온다", () => {
    renderCard(HOLD);

    fireEvent.click(screen.getByRole("button", { name: /상세 보기/ }));

    expect(screen.getByText(HOLD.detail as string)).toBeTruthy();
  });
});

describe("holdLast", () => {
  it("보류만 맨 뒤로 보내고 나머지 순서는 지킨다", () => {
    const list = [
      scenario(HOLD, "A"),
      scenario(UNVERIFIED, "B"),
      scenario(null, "C"),
      scenario(HOLD, "D"),
      scenario(RECORDED, "E"),
    ];

    expect(holdLast(list).map((s) => s.id)).toEqual(["B", "C", "E", "A", "D"]);
  });
});
