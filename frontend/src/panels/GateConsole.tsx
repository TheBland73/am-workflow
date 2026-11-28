import { useEffect, useMemo, useState } from "react";
import { Button, Input, Modal, Select, Tag, message } from "antd";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Gate, Snapshot } from "../api/types";
import {
  CHECKPOINT_LABEL, canJoinDiscussion, decidedByLabel, isSameFamilyDecision, SAME_FAMILY_HINT,
} from "./meetingLabels";

function durationText(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function asRows(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> =>
      Boolean(item) && typeof item === "object" && !Array.isArray(item))
    : [];
}

/** 名单里主持人提的临时角色：只在本场有效，没有角色键。 */
type TempRole = { key: string; display_name: string; persona: string };

export default function GateConsole({
  snapshot, gate, heldCount, onDecided,
}: {
  snapshot: Snapshot;
  gate: Gate | null;
  heldCount: number;
  onDecided: () => void;
}) {
  const client = useQueryClient();
  const [messageApi, holder] = message.useMessage();
  const [feedback, setFeedback] = useState("");
  const [target, setTarget] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState("");
  const [original, setOriginal] = useState("");
  const [answers, setAnswers] = useState<string[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const [initialRoles, setInitialRoles] = useState<string[]>([]);
  const [tempRoles, setTempRoles] = useState<TempRole[]>([]);
  const [initialTempRoles, setInitialTempRoles] = useState<TempRole[]>([]);

  const node = useMemo(
    () => snapshot.nodes.find((item) => item.id === gate?.node_id),
    [gate?.node_id, snapshot.nodes],
  );
  const checkpointKind = gate?.kind ?? "";
  const isCheckpoint = Boolean(checkpointKind);
  // 会议节点的 config.artifact 是会议产出的纪要，不是要在这里审的工件
  const artifactKind = isCheckpoint ? "" : String(node?.config?.artifact ?? "");
  const title = String(gate?.title ?? node?.config?.title ?? node?.name ?? "人工审批");
  const description = String(gate?.description ?? node?.config?.description ?? "");
  const payload = gate?.payload ?? {};
  const questions = Array.isArray(payload.questions) ? payload.questions.map(String) : [];
  const proposed = asRows(payload.proposed);

  const templates = useQuery({
    queryKey: ["templates"],
    queryFn: api.templates,
    enabled: Boolean(gate) && checkpointKind === "roster",
  });
  const roleOptions = useMemo(
    () => (templates.data?.items ?? [])
      .filter(canJoinDiscussion)
      .map((item) => ({ value: item.role_key, label: `${item.name}（${item.role_key}）` })),
    [templates.data],
  );

  useEffect(() => {
    setFeedback("");
    setEditing(false);
    setBusy(false);
    if (!gate) {
      setTarget(undefined);
      setBody("");
      setOriginal("");
      setAnswers([]);
      setRoles([]);
      setInitialRoles([]);
      setTempRoles([]);
      setInitialTempRoles([]);
      return;
    }
    const rejectTarget = String(node?.config?.reject_target ?? "");
    setTarget(rejectTarget || undefined);
    const kind = gate.kind ?? "";
    const data = gate.payload ?? {};
    setAnswers(Array.isArray(data.questions) ? data.questions.map(() => "") : []);
    const rows = asRows(data.proposed);
    const keys = rows
      .map((row) => String(row.role_key ?? ""))
      .filter(Boolean);
    const temporary = rows
      .filter((row) => !row.role_key)
      .map((row, index) => ({
        key: `tmp:${index}`,
        display_name: String(row.display_name ?? "").trim() || `临时角色 ${index + 1}`,
        persona: String(row.persona ?? ""),
      }));
    setRoles(keys);
    setInitialRoles(keys);
    setTempRoles(temporary);
    setInitialTempRoles(temporary);
    const artifact = kind ? "" : String(node?.config?.artifact ?? "");
    if (!artifact) {
      setBody("");
      setOriginal("");
      return;
    }
    api.getArtifact(snapshot.run.id, artifact)
      .then((detail) => {
        setBody(detail.body);
        setOriginal(detail.body);
      })
      .catch(() => {
        setBody("");
        setOriginal("");
      });
  }, [gate?.id, gate?.kind, snapshot.run.id]);

  const artifact = artifactKind
    ? snapshot.artifacts.find((item) => item.kind === artifactKind && item.status === "active")
    : undefined;
  const artifactEdited = Boolean(artifactKind) && body !== original;
  const waited = gate?.created_at
    ? (Date.now() - new Date(gate.created_at).getTime()) / 1000
    : 0;
  const rosterChanged = roles.join("\u0000") !== initialRoles.join("\u0000")
    || JSON.stringify(tempRoles) !== JSON.stringify(initialTempRoles);
  const rosterBounds = {
    min: Number(payload.min) || 0,
    max: Number(payload.max) || 0,
    fixed: Number(payload.fixed) || 0,
  };
  const rosterCount = roles.length + tempRoles.length;

  const refresh = () => {
    onDecided();
    client.invalidateQueries({ queryKey: ["snapshot", snapshot.run.id] });
  };

  /** 会议内确认点：决定只记在门上，驳回不需要目标节点。 */
  const decideCheckpoint = async (action: "approve" | "reject", option?: string) => {
    if (!gate) return;
    if (action === "reject" && !feedback.trim()) {
      messageApi.warning("请先写下意见");
      return;
    }
    if (checkpointKind === "roster" && action === "approve") {
      if (rosterCount === 0) {
        messageApi.warning("名单不能为空，请至少保留一名参会者");
        return;
      }
      if (rosterBounds.fixed > 0 && rosterCount !== rosterBounds.fixed) {
        messageApi.warning(`固定人数是 ${rosterBounds.fixed} 名，当前 ${rosterCount} 名`);
        return;
      }
      if (rosterBounds.fixed === 0
        && (rosterCount < rosterBounds.min || rosterCount > rosterBounds.max)) {
        messageApi.warning(
          `参会人数须在 ${rosterBounds.min} 到 ${rosterBounds.max} 名之间，当前 ${rosterCount} 名`);
        return;
      }
    }
    setBusy(true);
    try {
      if (checkpointKind === "roster" && action === "approve" && rosterChanged) {
        const { items } = await api.listMeetings(snapshot.run.id);
        const meeting = [...items].reverse()
          .find((item) => item.node_id === gate.node_id);
        if (!meeting) throw new Error("找不到这场会议的记录，无法改名单");
        // 不传 model_id：模板绑定的模型可能已禁用，让会议按角色默认/第一个可用模型回落
        await api.replaceParticipants(snapshot.run.id, meeting.id, [
          ...roles.map((key) => {
            const template = (templates.data?.items ?? [])
              .find((item) => item.role_key === key);
            return {
              role_key: key,
              display_name: template?.name ?? key,
              mode: "participant",
            };
          }),
          ...tempRoles.map((row) => ({
            role_key: null,
            display_name: row.display_name,
            persona: row.persona,
            mode: "participant",
          })),
        ]);
      }
      const data: Record<string, unknown> = {};
      if (option) data.option = option;
      if (checkpointKind === "clarify") {
        data.answers = questions.map((_, index) => (answers[index] ?? "").trim());
      }
      await api.decideGate(gate.id, action, {
        feedback: feedback.trim() || undefined,
        decided_by: "user",
        data,
      });
      messageApi.success(action === "approve" ? "已通过，会议继续" : "已驳回，会议继续");
      refresh();
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const decideArtifact = async (
    action: "approve" | "reject", data?: Record<string, unknown>,
  ) => {
    if (!gate) return;
    if (action === "reject" && (!feedback.trim() || !target)) {
      messageApi.warning("驳回必须填写反馈并指定重做节点");
      return;
    }
    setBusy(true);
    try {
      await api.decideGate(gate.id, action, {
        feedback: feedback.trim() || undefined,
        target_node: action === "reject" ? target : undefined,
        decided_by: "user",
        data,
        artifact_kind: action === "approve" && artifactEdited ? artifactKind : undefined,
        artifact_body: action === "approve" && artifactEdited ? body : undefined,
      });
      messageApi.success(action === "approve" ? "已通过" : "已驳回，相关节点将重做");
      refresh();
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const delegate = async () => {
    if (!gate) return;
    setBusy(true);
    try {
      await api.delegateGate(gate.id);
      messageApi.success("已交给模型，稍后刷新");
      refresh();
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const recentGates = snapshot.gates.filter((item) => item.status !== "open").slice(0, 5);
  const recentList = recentGates.length > 0 && (
    <ul className="ef-facts" style={{ display: "block", marginTop: 10 }}>
      <li className="ef-eyebrow">最近的确认点</li>
      {recentGates.map((item) => (
        <li key={item.id}>
          {item.kind
            ? CHECKPOINT_LABEL[item.kind] ?? item.kind
            : snapshot.nodes.find((node) => node.id === item.node_id)?.name ?? item.node_key}
          {" · "}
          {item.status === "approved" ? "通过" : item.status === "rejected" ? "驳回" : item.status}
          {" · 由"}
          {decidedByLabel(item.decided_by)}
          决定
          {isSameFamilyDecision(item.decision) ? ` · ${SAME_FAMILY_HINT}` : ""}
        </li>
      ))}
    </ul>
  );

  if (!gate || !node) {
    const last = snapshot.gates[0];
    return (
      <section className="ef-panel gatecard">
        {holder}
        <h2>审批 <span className="ef-tag">GATE</span></h2>
        <div className="gatecard-empty">
          {last
            ? `最近一次：${last.node_key} · ${last.status === "approved" ? "已通过" : last.status === "rejected" ? "已驳回" : last.status} · 由${decidedByLabel(last.decided_by)}决定`
            : "当前没有待审批的人工门。"}
          <br />
          运行进入人工门时会在这里出现裁决卡。
        </div>
        {recentList}
      </section>
    );
  }

  const gateNo = (() => {
    const gatesOnly = snapshot.nodes.filter((item) => item.type === "HUMAN_GATE");
    return gatesOnly.findIndex((item) => item.id === node.id) + 1;
  })();
  const badge = isCheckpoint
    ? CHECKPOINT_LABEL[checkpointKind] ?? checkpointKind
    : `GATE ${String(gateNo).padStart(2, "0")}`;

  const feedbackField = (placeholder: string) => (
    <label className="field">
      <span>裁决意见</span>
      <Input.TextArea
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
        placeholder={placeholder}
      />
    </label>
  );

  return (
    <section className="ef-panel gatecard">
      {holder}
      <h2>
        {title}
        <span className="ef-tag">{badge}</span>
      </h2>

      {!isCheckpoint && (
        <div className="artifact-line">
          <span className="ef-eyebrow">Artifact</span>
          <b>{artifactKind ? `${artifactKind}.md` : "无关联工件"}</b>
          <span className="ef-field">
            {artifact ? `v${artifact.version} · ${artifact.status.toUpperCase()}` : "—"}
          </span>
        </div>
      )}

      {!isCheckpoint && Boolean(node.config.escalation) && (
        <div className="ef-notice" data-flag="升级">
          {String(node.config.feedback ?? "该节点自动重试已达上限，已升级为人工决策。")}
        </div>
      )}
      {description && (
        <p
          style={{
            fontSize: "0.74rem",
            color: "var(--ef-ink-2)",
            margin: "0.45rem 0 0",
            whiteSpace: "pre-wrap",
          }}
        >
          {description}
        </p>
      )}

      {checkpointKind === "clarify" && (
        <div className="ef-form">
          {questions.map((question, index) => (
            <label className="field" key={`${index}-${question}`}>
              <span>{`${index + 1}. ${question}`}</span>
              <Input.TextArea
                value={answers[index] ?? ""}
                onChange={(event) => setAnswers((previous) => {
                  const next = [...previous];
                  next[index] = event.target.value;
                  return next;
                })}
                placeholder="没有答案就留空，会议会把它记为假设。"
                autoSize={{ minRows: 1, maxRows: 4 }}
              />
            </label>
          ))}
          <p className="ef-actions">
            <Button type="primary" loading={busy} onClick={() => decideCheckpoint("approve")}>
              提交回答并通过
            </Button>
            <Button loading={busy} onClick={delegate}>交给模型判</Button>
          </p>
        </div>
      )}

      {checkpointKind === "roster" && (
        <div className="ef-form">
          <label className="field">
            <span>主持人提议</span>
            <ul className="ef-facts" style={{ display: "block" }}>
              {proposed.length === 0 && <li>（没有提议，请从角色库里挑选）</li>}
              {proposed.map((row, index) => (
                <li key={`${String(row.role_key ?? "")}-${index}`}>
                  {String(row.display_name ?? row.role_key ?? "")}
                  {row.role_key ? "" : "（临时角色）"}
                  {row.reason ? `：${String(row.reason)}` : ""}
                </li>
              ))}
            </ul>
          </label>
          <label className="field">
            <span>参会角色</span>
            <Select
              mode="multiple"
              style={{ width: "100%" }}
              value={roles}
              onChange={setRoles}
              options={roleOptions}
              placeholder="增删角色；改了名单，通过时会先替换"
              loading={templates.isLoading}
            />
          </label>
          {tempRoles.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <span style={{ fontSize: 12, color: "var(--ef-ink-2)", marginRight: 8 }}>
                主持人提的临时角色（原样保留）
              </span>
              {tempRoles.map((row) => (
                <Tag
                  key={row.key}
                  closable
                  onClose={() => setTempRoles((previous) =>
                    previous.filter((item) => item.key !== row.key))}
                >
                  {row.display_name}
                </Tag>
              ))}
            </div>
          )}
          <p style={{ fontSize: 12, color: "var(--ef-ink-2)", margin: "0 0 8px" }}>
            {rosterBounds.fixed > 0
              ? `固定 ${rosterBounds.fixed} 名，当前 ${rosterCount} 名。`
              : `人数要求 ${rosterBounds.min} 到 ${rosterBounds.max} 名，当前 ${rosterCount} 名。`}
          </p>
          {rosterChanged && <Tag color="gold">名单已改动，通过时先替换参会者</Tag>}
          {feedbackField("驳回时写意见；通过可留空。")}
          <p className="ef-actions">
            <Button
              type="primary"
              loading={busy}
              disabled={rosterCount === 0}
              onClick={() => decideCheckpoint("approve")}
            >
              通过名单
            </Button>
            <Button danger loading={busy} onClick={() => decideCheckpoint("reject")}>
              驳回，让主持人重新提议
            </Button>
            <Button loading={busy} onClick={delegate}>交给模型判</Button>
          </p>
        </div>
      )}

      {checkpointKind === "consensus" && (
        <div className="ef-form">
          {feedbackField("选择「给指导语继续讨论」时必填；其余两项可选。")}
          <p className="ef-actions">
            <Button
              type="primary" loading={busy}
              onClick={() => decideCheckpoint("approve", "integrate")}
            >
              带着分歧整合
            </Button>
            <Button loading={busy} onClick={() => decideCheckpoint("approve", "delegate")}>
              授权主持人裁决
            </Button>
            <Button danger loading={busy} onClick={() => decideCheckpoint("reject", "continue")}>
              给指导语继续讨论
            </Button>
            <Button loading={busy} onClick={delegate}>交给模型判</Button>
          </p>
        </div>
      )}

      {checkpointKind === "escalation" && (
        <div className="ef-form">
          {feedbackField("驳回时写意见，会议带着它再讨论一轮。")}
          <p className="ef-actions">
            <Button type="primary" loading={busy} onClick={() => decideCheckpoint("approve")}>
              接受现状（通过）
            </Button>
            <Button danger loading={busy} onClick={() => decideCheckpoint("reject")}>
              驳回并再给一轮
            </Button>
            <Button loading={busy} onClick={delegate}>交给模型判</Button>
          </p>
        </div>
      )}

      {!isCheckpoint && (
        <div className="ef-form">
          {feedbackField("写给负责人的修改意见；驳回时必须填写。")}

          <div className="gate-select">
            <Select
              size="small"
              style={{ width: "100%" }}
              value={target}
              onChange={setTarget}
              placeholder="驳回后重做节点（驳回必选）"
              options={snapshot.nodes
                .filter((item) => item.id !== gate.node_id)
                .map((item) => ({
                  value: item.node_key,
                  label: `${item.name}（${item.node_key}）`,
                }))}
              allowClear
            />
          </div>

          <ul className="ef-facts">
            <li>已等待 {durationText(waited)}</li>
            <li>下游 {heldCount} 暂停</li>
            <li>{artifact ? `v${artifact.version}` : "无工件"}</li>
            <li>上次判定 {decidedByLabel(gate.decided_by)}</li>
            {artifactKind && (
              <li>
                <a
                  href="#"
                  onClick={(event) => {
                    event.preventDefault();
                    setEditing(true);
                  }}
                >
                  {artifactEdited ? "已改工件" : "编辑工件"}
                </a>
              </li>
            )}
          </ul>

          <p className="ef-actions">
            <Button type="primary" loading={busy} onClick={() => decideArtifact("approve")}>
              通过
            </Button>
            {node.node_key === "gate_proposal" && (
              <Button
                loading={busy}
                onClick={() => decideArtifact("approve", { route: "skip_review" })}
              >
                直接通过（跳过评审）
              </Button>
            )}
            <Button danger loading={busy} onClick={() => decideArtifact("reject")}>
              驳回
            </Button>
            <Button loading={busy} onClick={delegate}>交给模型判</Button>
          </p>
        </div>
      )}

      <p className="ef-notice" data-flag="暂停">
        {heldCount} 个下游节点暂停，等待本次裁决。
      </p>

      {recentList}

      <Modal
        open={editing}
        title={`编辑 ${artifactKind}.md`}
        width={820}
        onCancel={() => setEditing(false)}
        onOk={() => setEditing(false)}
        okText="完成编辑"
        cancelText="取消"
      >
        <p style={{ color: "var(--ef-ink-2)", fontSize: 12 }}>
          修改会在「通过」时作为新版本写入，并标记 approved。
        </p>
        <Input.TextArea
          value={body}
          onChange={(event) => setBody(event.target.value)}
          autoSize={{ minRows: 16, maxRows: 26 }}
          className="am-mono"
        />
        {artifactEdited && (
          <Tag color="gold" style={{ marginTop: 8 }}>已修改，通过时写新版本</Tag>
        )}
      </Modal>
    </section>
  );
}
