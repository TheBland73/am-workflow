import { useEffect, useState } from "react";
import { Button, Empty, Input, Select, Space, Tag, message } from "antd";
import { EditOutlined, SaveOutlined } from "@ant-design/icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ArtifactRecord } from "../api/types";

const KIND_LABEL: Record<string, string> = {
  idea: "想法", discussion: "会议纪要", proposal: "建设方案", review: "评审与确认报告",
  requirement: "需求规格", design: "设计规格", task: "任务清单", test: "测试记录",
  changelog: "变更日志",
};

export default function ArtifactsPanel({
  runId, artifacts,
}: { runId: string; artifacts: ArtifactRecord[] }) {
  const client = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const active = artifacts.filter((item) => item.status === "active");
  const [kind, setKind] = useState<string | undefined>(active[0]?.kind);
  const [body, setBody] = useState("");
  const [original, setOriginal] = useState("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!kind && active.length > 0) setKind(active[0].kind);
  }, [active, kind]);

  const detail = useQuery({
    queryKey: ["artifact", runId, kind],
    queryFn: () => api.getArtifact(runId, kind!),
    enabled: Boolean(kind),
  });

  useEffect(() => {
    if (detail.data) {
      setBody(detail.data.body);
      setOriginal(detail.data.body);
      setEditing(false);
    }
  }, [detail.data]);

  const save = async () => {
    if (!kind) return;
    setSaving(true);
    try {
      await api.patchArtifact(runId, kind, body, "reviewed");
      messageApi.success("已保存为人工新版本");
      setEditing(false);
      await client.invalidateQueries({ queryKey: ["artifact", runId, kind] });
      await client.invalidateQueries({ queryKey: ["snapshot", runId] });
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const record = active.find((item) => item.kind === kind);

  return (
    <div>
      {contextHolder}
      <Space style={{ marginBottom: 10, width: "100%", justifyContent: "space-between" }}>
        <Select
          size="small" style={{ minWidth: 220 }} value={kind} onChange={setKind}
          placeholder="选择工件"
          options={active.map((item) => ({
            value: item.kind,
            label: `${KIND_LABEL[item.kind] ?? item.kind}（${item.kind} v${item.version}）`,
          }))}
        />
        <Space size={4}>
          {record && <Tag color={record.status === "approved" ? "success" : "default"}>{record.status}</Tag>}
          {editing ? (
            <>
              <Button size="small" onClick={() => { setBody(original); setEditing(false); }}>取消</Button>
              <Button size="small" type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>
                保存新版本
              </Button>
            </>
          ) : (
            <Button size="small" icon={<EditOutlined />} disabled={!kind} onClick={() => setEditing(true)}>
              人工编辑
            </Button>
          )}
        </Space>
      </Space>

      {!kind && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有工件" />}
      {kind && detail.isLoading && <div style={{ color: "#6b7280" }}>加载中…</div>}
      {kind && detail.data && (
        <>
          <div style={{ fontSize: 12, color: "#6b7280", marginBottom: 6 }}>
            由节点 <span className="am-mono">{String(detail.data.meta.created_by ?? "—")}</span> 写入 ·
            {" "}v{String(detail.data.meta.version ?? record?.version ?? 1)} ·
            {" "}{String(detail.data.meta.status ?? "")}
          </div>
          {editing ? (
            <Input.TextArea
              value={body}
              onChange={(event) => setBody(event.target.value)}
              autoSize={{ minRows: 18, maxRows: 40 }}
              className="am-mono"
            />
          ) : (
            <pre className="am-artifact">{body}</pre>
          )}
          <div style={{ marginTop: 6, fontSize: 12, color: "#9ca3af" }}>
            人工编辑会写入新版本，旧版本自动标记 superseded；审批通过时工件状态转为 approved。
          </div>
        </>
      )}
    </div>
  );
}
