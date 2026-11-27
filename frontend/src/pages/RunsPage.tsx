import { useMemo, useState } from "react";
import {
  Button, Empty, Form, Input, InputNumber, Modal, Progress, Table, message,
} from "antd";
import { PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Run, RunStatus } from "../api/types";

const STATUS: Record<RunStatus, { color: string; label: string }> = {
  created: { color: "default", label: "已创建" },
  running: { color: "processing", label: "运行中" },
  waiting_human: { color: "warning", label: "等待人工" },
  finished: { color: "success", label: "已完成" },
  failed: { color: "error", label: "失败" },
  stopped: { color: "default", label: "已停止" },
};

const PHASE_LABELS: Record<string, string> = {
  idea: "想法",
  discussion: "讨论",
  proposal: "方案",
  requirement: "需求",
  design: "设计",
  tasks: "任务分解",
  execution: "执行",
  delivery: "交付",
};

export function StatusTag({ status }: { status: RunStatus }) {
  const item = STATUS[status] ?? { color: "default", label: status };
  return <span className={`status-pill is-${status}`}>{item.label}</span>;
}

/** 状态中文名：数据统计页复用同一份定义，避免两个页面说法不一致。 */
export function runStatusLabel(status: string): string {
  return STATUS[status as RunStatus]?.label ?? status;
}

export default function RunsPage() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm<{ idea: string; budget?: number }>();
  const [messageApi, contextHolder] = message.useMessage();

  const runs = useQuery({ queryKey: ["runs"], queryFn: api.listRuns, refetchInterval: 4000 });

  const create = useMutation({
    mutationFn: (values: { idea: string; budget?: number }) =>
      api.createRun(values.idea, "sdlc_v1", values.budget),
    onSuccess: (snapshot) => {
      setOpen(false);
      form.resetFields();
      client.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${snapshot.run.id}`);
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const columns = useMemo(() => [
    {
      title: "运行",
      dataIndex: "id",
      width: 150,
      render: (id: string) => <span className="am-mono">{id}</span>,
    },
    {
      title: "想法",
      dataIndex: "idea",
      ellipsis: true,
      render: (idea: string) => <span title={idea}>{idea}</span>,
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (status: RunStatus) => <StatusTag status={status} />,
    },
    { title: "当前阶段", dataIndex: "current_phase", width: 110,
      render: (phase: string) => PHASE_LABELS[phase] ?? phase ?? "—" },
    {
      title: "进度",
      width: 170,
      render: (_: unknown, run: Run) => {
        const total = run.nodes_total ?? 0;
        const done = run.nodes_done ?? 0;
        return (
          <Progress
            size="small"
            percent={total ? Math.round((done / total) * 100) : 0}
            format={() => `${done}/${total}`}
          />
        );
      },
    },
    { title: "创建时间", dataIndex: "created_at", width: 170,
      render: (value: string) => value?.replace("T", " ").slice(0, 19) },
    {
      title: "",
      width: 80,
      render: (_: unknown, run: Run) => (
        <Button size="small" onClick={() => navigate(`/runs/${run.id}`)}>打开</Button>
      ),
    },
  ], [navigate]);

  return (
    <div>
      {contextHolder}
      <div className="am-page-head">
        <div>
          <h1 className="am-page-title">运行</h1>
          <div className="am-page-sub">想法进入固定 SDLC 模板：讨论 → 方案 → 需求 → 设计 → 任务 → 并行开发 → 交付</div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<ReloadOutlined />} onClick={() => runs.refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            新建运行
          </Button>
        </div>
      </div>

      <div className="am-card" style={{ padding: 8 }}>
        <Table
          rowKey="id"
          size="middle"
          loading={runs.isLoading}
          dataSource={runs.data?.items ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有运行，点击右上角「新建运行」开始" /> }}
        />
      </div>

      <Modal
        title="新建运行"
        open={open}
        onCancel={() => setOpen(false)}
        confirmLoading={create.isPending}
        onOk={() => form.submit()}
        okText="创建并启动"
        width={640}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ budget: 1200000 }}
          onFinish={(values) => create.mutate(values)}
        >
          <Form.Item
            name="idea"
            label="想法"
            rules={[{ required: true, min: 10, message: "请至少写 10 个字，让讨论有输入" }]}
          >
            <Input.TextArea
              rows={6}
              placeholder="例：做一个面向小团队的「客户反馈收集与归类」内部工具：成员提交反馈，系统按主题聚类并给出优先级建议，支持导出周报。"
            />
          </Form.Item>
          <Form.Item name="budget" label="Token 预算">
            <InputNumber min={10000} step={100000} style={{ width: 220 }} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
