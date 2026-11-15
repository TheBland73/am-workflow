import { useEffect, useState } from "react";
import {
  Alert, Button, Descriptions, Input, Modal, Select, Space, Tag, Typography, message,
} from "antd";
import { CheckCircleOutlined, CloseCircleOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Gate, Snapshot } from "../api/types";

export default function GateCard({
  snapshot, gate, onClose, onDecided,
}: {
  snapshot: Snapshot;
  gate: Gate | null;
  onClose: () => void;
  onDecided: () => void;
}) {
  const [messageApi, contextHolder] = message.useMessage();
  const node = snapshot.nodes.find((item) => item.id === gate?.node_id);
  const kind = node?.config?.artifact as string | undefined;
  const escalation = Boolean(node?.config?.escalation);
  const [body, setBody] = useState("");
  const [original, setOriginal] = useState("");
  const [feedback, setFeedback] = useState("");
  const [target, setTarget] = useState<string | undefined>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setFeedback("");
    setTarget(undefined);
    if (!gate || !kind) { setBody(""); setOriginal(""); return; }
    api.getArtifact(snapshot.run.id, kind)
      .then((detail) => { setBody(detail.body); setOriginal(detail.body); })
      .catch(() => { setBody(""); setOriginal(""); });
  }, [gate?.id, kind, snapshot.run.id]);

  if (!gate) return null;

  const artifactEdited = Boolean(kind) && body !== original;

  const submit = async (action: "approve" | "reject") => {
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
        artifact_kind: artifactEdited ? kind : undefined,
        artifact_body: artifactEdited ? body : undefined,
      });
      messageApi.success(action === "approve" ? "已通过" : "已驳回，相关节点将重做");
      onDecided();
      onClose();
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const title = String(node?.config?.title ?? node?.name ?? "人工审批");
  const description = String(node?.config?.description ?? "");

  return (
    <Modal
      open onCancel={onClose} width={760} maskClosable={false}
      title={<Space><Tag color="warning">人工门</Tag>{title}</Space>}
      footer={
        <Space>
          <Button onClick={onClose}>稍后处理</Button>
          <Button danger icon={<CloseCircleOutlined />} loading={busy} onClick={() => submit("reject")}>
            驳回
          </Button>
          <Button type="primary" icon={<CheckCircleOutlined />} loading={busy} onClick={() => submit("approve")}>
            通过
          </Button>
        </Space>
      }
    >
      {contextHolder}
      {escalation && (
        <Alert
          type="warning" showIcon style={{ marginBottom: 12 }}
          message="该节点自动重试已达上限，已升级为人工决策"
          description={String(node?.config?.feedback ?? "")}
        />
      )}
      {description && (
        <Typography.Paragraph type="secondary" style={{ marginBottom: 8 }}>
          {description}
        </Typography.Paragraph>
      )}
      <Descriptions
        size="small" column={2} style={{ marginBottom: 12 }}
        items={[
          { key: "gate", label: "门 ID", children: gate.id },
          { key: "node", label: "节点", children: node?.node_key ?? "—" },
          { key: "kind", label: "关联工件", children: kind ?? "—" },
          { key: "status", label: "状态", children: gate.status },
        ]}
      />

      {kind && (
        <div style={{ marginBottom: 12 }}>
          <div style={{ fontWeight: 600, margin: "4px 0 6px" }}>
            工件内容（可直接修改，通过时保存为新版本并标记 approved）
          </div>
          <Input.TextArea
            value={body}
            onChange={(event) => setBody(event.target.value)}
            autoSize={{ minRows: 12, maxRows: 24 }}
            className="am-mono"
          />
          {artifactEdited && <Tag color="blue" style={{ marginTop: 6 }}>已修改，将写入新版本</Tag>}
        </div>
      )}

      <div style={{ fontWeight: 600, marginBottom: 6 }}>审批意见</div>
      <Input.TextArea
        value={feedback}
        onChange={(event) => setFeedback(event.target.value)}
        autoSize={{ minRows: 2, maxRows: 5 }}
        placeholder="通过时可留空；驳回时必填，例如：缺少导出功能的验收标准"
      />
      <div style={{ marginTop: 8 }}>
        <span style={{ marginRight: 8, color: "#6b7280" }}>驳回后重做节点</span>
        <Select
          style={{ minWidth: 320 }} value={target} onChange={setTarget}
          placeholder="选择要重做并上传反馈的节点"
          options={snapshot.nodes
            .filter((item) => item.id !== gate.node_id)
            .map((item) => ({
              value: item.node_key,
              label: `${item.name}（${item.node_key}）`,
            }))}
        />
      </div>
      <div style={{ marginTop: 8, fontSize: 12, color: "#9ca3af" }}>
        驳回会把目标节点及其全部下游重置为 pending，并在目标节点重跑时注入这条反馈。
      </div>
    </Modal>
  );
}
