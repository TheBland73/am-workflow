import { useState } from "react";
import {
  Button, Form, Input, InputNumber, Modal, Select, Space, Table, message,
} from "antd";
import { EditOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AgentTemplate } from "../api/types";

const SCOPE_LABEL: Record<string, string> = {
  agent: "L1 角色私有", session: "L0 本次会话", project: "L2 项目", global: "L3 平台",
};

export default function TemplatesPage() {
  const client = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [modal, setModal] = useState<{ open: boolean; edit?: AgentTemplate }>({ open: false });
  const [promptView, setPromptView] = useState<{ open: boolean; row?: AgentTemplate }>({ open: false });
  const [form] = Form.useForm();

  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });

  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      api.patchTemplate(modal.edit!.id, values),
    onSuccess: () => {
      setModal({ open: false });
      client.invalidateQueries({ queryKey: ["templates"] });
      messageApi.success("已保存");
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const modelOptions = (models.data?.items ?? []).map((model) => ({
    value: model.id, label: `${model.display_name}（${model.model_name}）`,
  }));

  return (
    <div>
      {contextHolder}
      <div className="am-page-head">
        <div>
          <h1 className="am-page-title">角色模板</h1>
          <div className="am-page-sub">
            角色（谁）与模型（用哪个模型）解耦：模板绑定默认模型，运行时可覆盖。
            系统提示词里只写这个角色的职责，平台规则由引擎统一注入。
          </div>
        </div>
      </div>

      <div className="am-card" style={{ padding: 8 }}>
        <div className="tpl-toolbar">
          <span>
            共 {templates.data?.items.length ?? 0} 个角色
            <em>模型绑定后，运行时优先用该角色绑定的模型</em>
          </span>
          <span className="tpl-toolbar-hint">滚轮滚动列表 · 点击提示词查看全文</span>
        </div>
        <Table
          rowKey="id" size="middle" loading={templates.isLoading}
          className="tpl-table"
          dataSource={templates.data?.items ?? []} pagination={false}
          scroll={{ y: "calc(100vh - 400px)" }}
          columns={[
            { title: "角色", dataIndex: "name", width: 150,
              render: (name: string, row: AgentTemplate) => (
                <div>
                  <div style={{ fontWeight: 600 }}>{name}</div>
                  <div className="am-mono" style={{ fontSize: 11, color: "#9ca3af" }}>{row.role_key}</div>
                </div>
              ) },
            { title: "系统提示词", dataIndex: "system_prompt", ellipsis: true,
              render: (prompt: string, row: AgentTemplate) => (
                <span
                  className="tpl-prompt"
                  title="点击查看全文"
                  onClick={() => setPromptView({ open: true, row })}
                >
                  {prompt}
                </span>
              ) },
            { title: "工具", dataIndex: "tools_json", width: 190,
              render: (tools: string[]) => tools.length
                ? <Space size={2} wrap>{tools.map((tool) => <span className="ef-tag" key={tool}>{tool}</span>)}</Space>
                : <span style={{ color: "var(--ef-ink-2)" }}>无</span> },
            { title: "记忆范围", dataIndex: "memory_scope", width: 110,
              render: (scope: string) => <span className="ef-tag">{SCOPE_LABEL[scope] ?? scope}</span> },
            { title: "默认模型", dataIndex: "default_model_id", width: 210,
              render: (id: number | null) => {
                const model = (models.data?.items ?? []).find((item) => item.id === id);
                return model
                  ? <span>{model.display_name}</span>
                  : <span style={{ color: "var(--ef-ink-2)" }}>未绑定 · 取第一个可用模型</span>;
              } },
            { title: "最大步数", dataIndex: "max_steps", width: 90 },
            { title: "", width: 50,
              render: (_: unknown, row: AgentTemplate) => (
                <Button
                  size="small" type="text" icon={<EditOutlined />}
                  onClick={() => {
                    setModal({ open: true, edit: row });
                    form.setFieldsValue({
                      default_model_id: row.default_model_id,
                      max_steps: row.max_steps,
                      system_prompt: row.system_prompt,
                      memory_scope: row.memory_scope,
                    });
                  }}
                />
              ) },
          ]}
        />
      </div>

      <Modal
        title={`编辑角色：${modal.edit?.name ?? ""}`}
        open={modal.open} width={720}
        onCancel={() => setModal({ open: false })}
        onOk={() => form.submit()}
        confirmLoading={save.isPending}
      >
        <Form form={form} layout="vertical" onFinish={(values) => save.mutate(values)}>
          <Space align="start" style={{ display: "flex" }}>
            <Form.Item name="default_model_id" label="默认模型" style={{ width: 320 }}>
              <Select allowClear options={modelOptions} placeholder="不绑定则用第一个可用模型" />
            </Form.Item>
            <Form.Item name="max_steps" label="最大步数" style={{ width: 140 }}>
              <InputNumber min={1} max={64} />
            </Form.Item>
            <Form.Item name="memory_scope" label="记忆范围" style={{ width: 170 }}>
              <Select options={Object.entries(SCOPE_LABEL).map(([value, label]) => ({ value, label }))} />
            </Form.Item>
          </Space>
          <Form.Item name="system_prompt" label="系统提示词">
            <Input.TextArea autoSize={{ minRows: 10, maxRows: 22 }} className="am-mono" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={`系统提示词：${promptView.row?.name ?? ""}`}
        open={promptView.open}
        width={780}
        onCancel={() => setPromptView({ open: false })}
        footer={<Button onClick={() => setPromptView({ open: false })}>关闭</Button>}
      >
        <pre className="tpl-prompt-body">{promptView.row?.system_prompt || "（空）"}</pre>
      </Modal>
    </div>
  );
}
