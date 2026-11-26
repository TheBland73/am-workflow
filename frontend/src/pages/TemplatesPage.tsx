import { useState } from "react";
import {
  Button, Checkbox, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Table, Tag,
  message,
} from "antd";
import { DeleteOutlined, EditOutlined, PlusOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { AgentTemplate } from "../api/types";

const SCOPE_LABEL: Record<string, string> = {
  agent: "L1 角色私有", session: "L0 本次会话", project: "L2 项目", global: "L3 平台",
};

const MEETING_ROLE_OPTIONS = [
  { value: "discussion", label: "讨论" },
  { value: "review", label: "评审" },
];

export default function TemplatesPage() {
  const client = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [modal, setModal] = useState<{ open: boolean; edit?: AgentTemplate }>({ open: false });
  const [creating, setCreating] = useState(false);
  const [promptView, setPromptView] = useState<{ open: boolean; row?: AgentTemplate }>({ open: false });
  const [form] = Form.useForm();
  const [createForm] = Form.useForm();

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

  const create = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const meta = modalMeta(values);
      return api.createTemplate({
        role_key: String(values.role_key ?? "").trim(),
        name: String(values.name ?? "").trim(),
        system_prompt: String(values.system_prompt ?? ""),
        meta,
      });
    },
    onSuccess: () => {
      setCreating(false);
      createForm.resetFields();
      client.invalidateQueries({ queryKey: ["templates"] });
      messageApi.success("已新建角色");
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const remove = useMutation({
    mutationFn: (row: AgentTemplate) => api.deleteTemplate(row.id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["templates"] });
      messageApi.success("已删除自定义角色");
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
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => { setCreating(true); createForm.resetFields(); }}
        >
          新建角色
        </Button>
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
            { title: "角色", dataIndex: "name", width: 200,
              render: (name: string, row: AgentTemplate) => (
                <div>
                  <div style={{ fontWeight: 600 }}>
                    {name}
                    <Tag style={{ marginLeft: 6 }} color={row.builtin ? "default" : "blue"}>
                      {row.builtin ? "内置" : "自定义"}
                    </Tag>
                  </div>
                  <div className="am-mono" style={{ fontSize: 11, color: "#9ca3af" }}>{row.role_key}</div>
                </div>
              ) },
            { title: "简介", dataIndex: ["meta", "description"], width: 240, ellipsis: true,
              render: (_: unknown, row: AgentTemplate) => (
                <span title={row.meta?.description}>
                  {row.meta?.description || <span style={{ color: "var(--ef-ink-2)" }}>—</span>}
                </span>
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
            { title: "", width: 90,
              render: (_: unknown, row: AgentTemplate) => (
                <Space size={2}>
                  <Button
                    size="small" type="text" icon={<EditOutlined />}
                    onClick={() => {
                      setModal({ open: true, edit: row });
                      form.setFieldsValue({
                        default_model_id: row.default_model_id,
                        max_steps: row.max_steps,
                        system_prompt: row.system_prompt,
                        memory_scope: row.memory_scope,
                        description: row.meta?.description ?? "",
                        tags: row.meta?.tags ?? [],
                      });
                    }}
                  />
                  {!row.builtin && (
                    <Popconfirm
                      title="删除这个自定义角色？"
                      description="已开始运行里的会议不受影响，之后不能再点名它。"
                      okText="删除" cancelText="取消"
                      onConfirm={() => remove.mutate(row)}
                    >
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  )}
                </Space>
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
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => save.mutate({
            ...values,
            meta: {
              ...(modal.edit?.meta ?? {}),
              description: String(values.description ?? "").trim(),
              tags: values.tags ?? [],
            },
          })}
        >
          <Space align="start" style={{ display: "flex" }}>
            <Form.Item name="default_model_id" label="默认模型" style={{ width: 300 }}>
              <Select allowClear options={modelOptions} placeholder="不绑定则用第一个可用模型" />
            </Form.Item>
            <Form.Item name="max_steps" label="最大步数" style={{ width: 120 }}>
              <InputNumber min={1} max={64} />
            </Form.Item>
            <Form.Item name="memory_scope" label="记忆范围" style={{ width: 170 }}>
              <Select options={Object.entries(SCOPE_LABEL).map(([value, label]) => ({ value, label }))} />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="一句话简介">
            <Input placeholder="主持人挑名单时读的就是这一句" maxLength={200} />
          </Form.Item>
          <Form.Item name="tags" label="专长标签">
            <Select mode="tags" placeholder="回车添加标签" />
          </Form.Item>
          <Form.Item name="system_prompt" label="系统提示词">
            <Input.TextArea autoSize={{ minRows: 10, maxRows: 22 }} className="am-mono" />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="新建角色"
        open={creating} width={720}
        onCancel={() => setCreating(false)}
        onOk={() => createForm.submit()}
        confirmLoading={create.isPending}
        okText="新建"
      >
        <Form
          form={createForm}
          layout="vertical"
          initialValues={{ meeting_roles: [] }}
          onFinish={(values) => create.mutate(values)}
        >
          <Space align="start" style={{ display: "flex" }}>
            <Form.Item
              name="role_key" label="角色键" style={{ width: 220 }}
              rules={[
                { required: true, message: "请填写角色键" },
                { pattern: /^[a-z][a-z0-9_]+$/, message: "小写字母开头，只含小写字母、数字、下划线" },
              ]}
              extra="建成后不可修改，运行配置按它点名角色。"
            >
              <Input placeholder="例：data_privacy" className="am-mono" />
            </Form.Item>
            <Form.Item
              name="name" label="名称" style={{ width: 220 }}
              rules={[{ required: true, message: "请填写名称" }]}
            >
              <Input placeholder="例：数据隐私" />
            </Form.Item>
          </Space>
          <Form.Item name="description" label="一句话简介">
            <Input placeholder="主持人挑名单时读的就是这一句" maxLength={200} />
          </Form.Item>
          <Space align="start" style={{ display: "flex" }}>
            <Form.Item name="tags" label="专长标签" style={{ width: 320 }}>
              <Select mode="tags" placeholder="回车添加标签" />
            </Form.Item>
            <Form.Item name="meeting_roles" label="适合的会议角色">
              <Checkbox.Group options={MEETING_ROLE_OPTIONS} />
            </Form.Item>
          </Space>
          <Form.Item
            name="system_prompt" label="系统提示词"
            rules={[{ required: true, message: "请写提示词" }]}
          >
            <Input.TextArea
              autoSize={{ minRows: 8, maxRows: 18 }}
              className="am-mono"
              placeholder="写具体的偏好，不要只写标签。例：优先保证可验证性，对没有验收标准的条目一律反对。"
            />
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

/** 新建角色时提交的元数据：一句话简介、专长标签、适合的会议角色。 */
function modalMeta(values: Record<string, unknown>) {
  return {
    description: String(values.description ?? "").trim(),
    tags: (values.tags as string[] | undefined) ?? [],
    meeting_roles: (values.meeting_roles as string[] | undefined) ?? [],
  };
}
