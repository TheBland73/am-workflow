import { useState } from "react";
import {
  Button, Empty, Form, Input, Modal, Popconfirm, Select, Space, Table, Tag, message,
} from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Task } from "../api/types";

const STATUS = ["pending", "running", "done", "failed", "skipped"];

export default function TasksPanel({ runId, tasks }: { runId: string; tasks: Task[] }) {
  const client = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();

  const refresh = () => client.invalidateQueries({ queryKey: ["snapshot", runId] });

  const update = useMutation({
    mutationFn: ({ uid, body }: { uid: string; body: Partial<Task> }) =>
      api.patchTask(runId, uid, body),
    onSuccess: refresh,
    onError: (error: Error) => messageApi.error(error.message),
  });

  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => api.createTask(runId, {
      task_uid: String(values.task_uid),
      title: String(values.title),
      deps: String(values.deps ?? "").split(",").map((item) => item.trim()).filter(Boolean),
      design_section: values.design_section ? String(values.design_section) : null,
      test_cases: String(values.test_cases ?? "").split(",").map((item) => item.trim()).filter(Boolean),
      status: "pending",
    }),
    onSuccess: () => { setOpen(false); form.resetFields(); refresh(); },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (uid: string) => api.deleteTask(runId, uid),
    onSuccess: refresh,
    onError: (error: Error) => messageApi.error(error.message),
  });

  return (
    <div>
      {contextHolder}
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={{ color: "#6b7280", fontSize: 12 }}>
          共 {tasks.length} 个任务，依赖变化时由后端重算波次
        </span>
        <Button size="small" icon={<PlusOutlined />} onClick={() => setOpen(true)}>新增任务</Button>
      </div>
      {tasks.length === 0
        ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="任务图尚未生成" />
        : (
          <Table
            rowKey="task_uid" size="small" pagination={false} dataSource={tasks}
            columns={[
              { title: "ID", dataIndex: "task_uid", width: 80,
                render: (uid: string) => <span className="am-mono">{uid}</span> },
              { title: "任务", dataIndex: "title", ellipsis: true },
              { title: "依赖", dataIndex: "deps", width: 100,
                render: (deps: string[]) => deps.length ? deps.join(", ") : "—" },
              { title: "波次", dataIndex: "wave", width: 64,
                render: (wave: number | null) => wave ?? "—" },
              { title: "状态", dataIndex: "status", width: 120,
                render: (status: string, task: Task) => (
                  <Space size={4}>
                    <Tag>{status}</Tag>
                    <Select
                      size="small" style={{ width: 96 }} value={status}
                      options={STATUS.map((item) => ({ value: item, label: item }))}
                      onChange={(value) => update.mutate({ uid: task.task_uid, body: { status: value } })}
                    />
                  </Space>
                ) },
              { title: "", width: 44,
                render: (_: unknown, task: Task) => (
                  <Popconfirm title="删除任务？" onConfirm={() => remove.mutate(task.task_uid)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                ) },
            ]}
          />
        )}

      <Modal
        title="新增任务" open={open} onCancel={() => setOpen(false)}
        onOk={() => form.submit()} confirmLoading={create.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(values) => create.mutate(values)}>
          <Form.Item name="task_uid" label="任务 ID" rules={[{ required: true }]}>
            <Input placeholder="T3" />
          </Form.Item>
          <Form.Item name="title" label="标题" rules={[{ required: true }]}>
            <Input placeholder="实现导出接口" />
          </Form.Item>
          <Form.Item name="deps" label="依赖（逗号分隔）">
            <Input placeholder="T1, T2" />
          </Form.Item>
          <Form.Item name="design_section" label="设计章节">
            <Input placeholder="3.2" />
          </Form.Item>
          <Form.Item name="test_cases" label="测试用例（逗号分隔）">
            <Input placeholder="TC-01, TC-02" />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
