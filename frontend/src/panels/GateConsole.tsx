import { useEffect, useMemo, useState } from "react";
import { Button, Input, Modal, Select, Tag, message } from "antd";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Gate, Snapshot } from "../api/types";

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

  const node = useMemo(
    () => snapshot.nodes.find((item) => item.id === gate?.node_id),
    [gate?.node_id, snapshot.nodes],
  );
  const kind = (node?.config?.artifact as string) ?? "";
  const escalation = Boolean(node?.config?.escalation);
  const title = String(node?.config?.title ?? node?.name ?? "人工审批");
  const description = String(node?.config?.description ?? "");

  useEffect(() => {
    setFeedback("");
    setTarget(undefined);
    setEditing(false);
    if (!gate || !kind) {
      setBody("");
      setOriginal("");
      return;
    }
    api.getArtifact(snapshot.run.id, kind)
      .then((detail) => {
        setBody(detail.body);
        setOriginal(detail.body);
      })
      .catch(() => {
        setBody("");
        setOriginal("");
      });
  }, [gate?.id, kind, snapshot.run.id]);

  const artifact = snapshot.artifacts.find((item) => item.kind === kind && item.status === "active");
  const waited = gate?.created_at
    ? (Date.now() - new Date(gate.created_at).getTime()) / 1000
    : 0;

  const submit = async (action: "approve" | "reject") => {
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
        artifact_kind: action === "approve" && body !== original && kind ? kind : undefined,
        artifact_body: action === "approve" && body !== original && kind ? body : undefined,
      });
      messageApi.success(action === "approve" ? "已通过" : "已驳回，相关节点将重做");
      onDecided();
      client.invalidateQueries({ queryKey: ["snapshot", snapshot.run.id] });
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!gate || !node) {
    const last = snapshot.gates[0];
    return (
      <section className="ef-panel gatecard">
        {holder}
        <h2>审批 <span className="ef-tag">GATE</span></h2>
        <div className="gatecard-empty">
          {last
            ? `最近一次：${last.node_key} · ${last.status === "approved" ? "已通过" : last.status === "rejected" ? "已驳回" : last.status}`
            : "当前没有待审批的人工门。"}
          <br />
          运行进入人工门时会在这里出现裁决卡。
        </div>
      </section>
    );
  }

  const gateNo = (() => {
    const gatesOnly = snapshot.nodes.filter((item) => item.type === "HUMAN_GATE");
    return gatesOnly.findIndex((item) => item.id === node.id) + 1;
  })();

  return (
    <section className="ef-panel gatecard">
      {holder}
      <h2>
        {title}
        <span className="ef-tag">GATE {String(gateNo).padStart(2, "0")}</span>
      </h2>

      <div className="artifact-line">
        <span className="ef-eyebrow">Artifact</span>
        <b>{kind ? `${kind}.md` : "无关联工件"}</b>
        <span className="ef-field">
          {artifact ? `v${artifact.version} · ${artifact.status.toUpperCase()}` : "—"}
        </span>
      </div>

      {escalation && (
        <div className="ef-notice" data-flag="升级">
          {String(node.config.feedback ?? "该节点自动重试已达上限，已升级为人工决策。")}
        </div>
      )}
      {description && (
        <p style={{ fontSize: "0.74rem", color: "var(--ef-ink-2)", margin: "0.45rem 0 0" }}>
          {description}
        </p>
      )}

      <div className="ef-form">
        <label className="field">
          <span>裁决意见</span>
          <Input.TextArea
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="写给负责人的修改意见；驳回时必须填写。"
          />
        </label>

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
          {kind && (
            <li>
              <a
                href="#"
                onClick={(event) => {
                  event.preventDefault();
                  setEditing(true);
                }}
              >
                {body !== original ? "已改工件" : "编辑工件"}
              </a>
            </li>
          )}
        </ul>

        <p className="ef-actions">
          <Button type="primary" loading={busy} onClick={() => submit("approve")}>
            通过
          </Button>
          <Button danger loading={busy} onClick={() => submit("reject")}>
            驳回
          </Button>
        </p>
      </div>

      <p className="ef-notice" data-flag="暂停">
        {heldCount} 个下游节点暂停，等待本次裁决。
      </p>

      <Modal
        open={editing}
        title={`编辑 ${kind}.md`}
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
        {body !== original && (
          <Tag color="gold" style={{ marginTop: 8 }}>已修改，通过时写新版本</Tag>
        )}
      </Modal>
    </section>
  );
}
