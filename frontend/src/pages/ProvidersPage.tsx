import { useMemo, useState } from "react";
import {
  Button, Form, Input, InputNumber, Modal, Popconfirm, Select, Space, Switch,
  Table, message,
} from "antd";
import {
  ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined, ThunderboltOutlined,
} from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api/client";
import type { ModelEntry, Provider } from "../api/types";

const PROTOCOLS = [
  { value: "openai_chat", label: "openai_chat（OpenAI 兼容）" },
  { value: "anthropic_messages", label: "anthropic_messages" },
  { value: "gemini_generate", label: "gemini_generate" },
  { value: "custom", label: "custom" },
];

export default function ProvidersPage() {
  const client = useQueryClient();
  const [messageApi, contextHolder] = message.useMessage();
  const [providerModal, setProviderModal] = useState<{ open: boolean; edit?: Provider }>({ open: false });
  const [modelModal, setModelModal] = useState<{ open: boolean; providerId?: number; edit?: ModelEntry }>({ open: false });
  const [form] = Form.useForm();
  const [modelForm] = Form.useForm();

  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const presets = useQuery({ queryKey: ["presets"], queryFn: api.presets });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["providers"] });
    client.invalidateQueries({ queryKey: ["models"] });
  };

  const saveProvider = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const edit = providerModal.edit;
      const body: Record<string, unknown> = {
        name: values.name, protocol: values.protocol, base_url: values.base_url,
        timeout_s: values.timeout_s, max_concurrency: values.max_concurrency,
        enabled: values.enabled,
      };
      if (values.api_key) body.api_key = values.api_key;
      return edit ? api.patchProvider(edit.id, body) : api.createProvider(body);
    },
    onSuccess: () => { setProviderModal({ open: false }); form.resetFields(); refresh(); },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const saveModel = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const body = { ...values, provider_id: modelModal.providerId ?? modelModal.edit?.provider_id };
      return modelModal.edit ? api.patchModel(modelModal.edit.id, body) : api.createModel(body);
    },
    onSuccess: () => { setModelModal({ open: false }); modelForm.resetFields(); refresh(); },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const removeProvider = useMutation({
    mutationFn: (id: number) => api.deleteProvider(id),
    onSuccess: refresh,
    onError: (error: Error) => messageApi.error(error.message),
  });
  const removeModel = useMutation({
    mutationFn: (id: number) => api.deleteModel(id),
    onSuccess: refresh,
    onError: (error: Error) => messageApi.error(error.message),
  });
  const test = useMutation({
    mutationFn: (id: number) => api.testProvider(id),
    onSuccess: (result) => messageApi[result.ok ? "success" : "error"](result.message),
    onError: (error: Error) => messageApi.error(error.message),
  });

  const modelOptions = useMemo(
    () => (models.data?.items ?? []).map((model) => ({
      value: model.id, label: `${model.display_name}（${model.model_name}）`,
    })),
    [models.data],
  );

  const openCreate = () => {
    setProviderModal({ open: true });
    form.setFieldsValue({
      protocol: "openai_chat", timeout_s: 60, max_concurrency: 2, enabled: true, name: "",
      base_url: "", api_key: "",
    });
  };

  const openEdit = (provider: Provider) => {
    setProviderModal({ open: true, edit: provider });
    form.setFieldsValue({
      name: provider.name, protocol: provider.protocol, base_url: provider.base_url,
      api_key: "", timeout_s: provider.timeout_s,
      max_concurrency: provider.max_concurrency, enabled: provider.enabled,
    });
  };

  return (
    <div>
      {contextHolder}
      <div className="am-page-head">
        <div>
          <h1 className="am-page-title">供应商与模型</h1>
          <div className="am-page-sub">
            供应商只描述「怎么连」，模型只描述「调什么、有什么能力」，角色模板再绑定到具体模型。
            密钥经 Fernet 加密落盘，接口只回显末四位。
          </div>
        </div>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>新增供应商</Button>
      </div>

      <div className="am-card" style={{ padding: 8 }}>
        <Table
          rowKey="id"
          size="middle"
          loading={providers.isLoading}
          dataSource={providers.data?.items ?? []}
          pagination={false}
          expandable={{
            expandedRowRender: (provider) => {
              const rows = (models.data?.items ?? []).filter((model) => model.provider_id === provider.id);
              return (
                <div style={{ padding: "4px 12px 8px" }}>
                  <div style={{ marginBottom: 8, display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "#6b7280", fontSize: 12 }}>该供应商下的模型</span>
                    <Button
                      size="small" icon={<PlusOutlined />}
                      onClick={() => {
                        setModelModal({ open: true, providerId: provider.id });
                        modelForm.setFieldsValue({
                          context_window: 32000, max_output_tokens: 4096,
                          cap_tools: true, cap_json: true, cap_vision: false, enabled: true,
                        });
                      }}
                    >
                      新增模型
                    </Button>
                  </div>
                  <Table
                    rowKey="id" size="small" pagination={false} dataSource={rows}
                    columns={[
                      { title: "模型名", dataIndex: "model_name", render: (name: string) => <span className="am-mono">{name}</span> },
                      { title: "显示名", dataIndex: "display_name" },
                      { title: "上下文", dataIndex: "context_window", width: 110,
                        render: (value: number) => value?.toLocaleString() },
                      { title: "最大输出", dataIndex: "max_output_tokens", width: 110,
                        render: (value: number) => value?.toLocaleString() },
                      {
                        title: "能力", width: 220,
                        render: (_: unknown, model: ModelEntry) => (
                          <Space size={4}>
                            {model.cap_tools && <span className="ef-tag">tools</span>}
                            {model.cap_json && <span className="ef-tag">json</span>}
                            {model.cap_vision && <span className="ef-tag">vision</span>}
                            {!model.enabled && <span className="ef-tag">disabled</span>}
                          </Space>
                        ),
                      },
                      {
                        title: "", width: 90,
                        render: (_: unknown, model: ModelEntry) => (
                          <Space size={2}>
                            <Button
                              size="small" type="text" icon={<EditOutlined />}
                              onClick={() => {
                                setModelModal({ open: true, edit: model });
                                modelForm.setFieldsValue(model);
                              }}
                            />
                            <Popconfirm title="删除模型？" onConfirm={() => removeModel.mutate(model.id)}>
                              <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                </div>
              );
            },
          }}
          columns={[
            { title: "名称", dataIndex: "name" },
            { title: "协议", dataIndex: "protocol", width: 170,
              render: (protocol: string) => <span className="am-mono">{protocol}</span> },
            { title: "Base URL", dataIndex: "base_url", ellipsis: true },
            { title: "密钥", dataIndex: "api_key_masked", width: 130,
              render: (masked: string) => masked ? <span className="am-mono">{masked}</span> : <span className="ef-tag">未配置</span> },
            { title: "并发", dataIndex: "max_concurrency", width: 70 },
            { title: "模型", dataIndex: "models_count", width: 70 },
            { title: "启用", dataIndex: "enabled", width: 80,
              render: (enabled: boolean) => enabled
                ? <span className="ef-tag is-signal">启用</span>
                : <span className="ef-tag">停用</span> },
            {
              title: "", width: 190,
              render: (_: unknown, provider: Provider) => (
                <Space size={2}>
                  <Button
                    size="small" icon={<ThunderboltOutlined />}
                    loading={test.isPending && test.variables === provider.id}
                    onClick={() => test.mutate(provider.id)}
                  >
                    测试
                  </Button>
                  <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(provider)} />
                  <Popconfirm title="删除供应商及其模型？" onConfirm={() => removeProvider.mutate(provider.id)}>
                    <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                  </Popconfirm>
                </Space>
              ),
            },
          ]}
        />
      </div>

      <Modal
        title={providerModal.edit ? "编辑供应商" : "新增供应商"}
        open={providerModal.open}
        onCancel={() => setProviderModal({ open: false })}
        onOk={() => form.submit()}
        confirmLoading={saveProvider.isPending}
        width={640}
      >
        <Form
          form={form} layout="vertical"
          onFinish={(values) => saveProvider.mutate(values)}
        >
          {!providerModal.edit && (
            <Form.Item label="从预设填充">
              <Select
                allowClear placeholder="选择预设（可再手改）"
                options={(presets.data?.presets ?? []).map((preset) => ({
                  value: preset.key,
                  label: `${preset.name} · ${preset.protocol}`,
                }))}
                onChange={(key) => {
                  const preset = presets.data?.presets.find((item) => item.key === key);
                  if (preset) {
                    form.setFieldsValue({
                      name: preset.name, protocol: preset.protocol, base_url: preset.base_url,
                    });
                  }
                }}
              />
            </Form.Item>
          )}
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="name" label="名称" rules={[{ required: true }]} style={{ width: 260 }}>
              <Input placeholder="DeepSeek" />
            </Form.Item>
            <Form.Item name="protocol" label="协议" rules={[{ required: true }]} style={{ width: 260 }}>
              <Select options={PROTOCOLS} />
            </Form.Item>
          </Space>
          <Form.Item name="base_url" label="Base URL" rules={[{ required: true }]}>
            <Input placeholder="https://api.deepseek.com/v1" />
          </Form.Item>
          <Form.Item
            name="api_key"
            label={providerModal.edit ? "API Key（留空=不变，填 - 清空）" : "API Key"}
          >
            <Input.Password placeholder={providerModal.edit ? "留空保持不变" : "sk-..."} />
          </Form.Item>
          <Space align="start">
            <Form.Item name="timeout_s" label="超时（秒）" style={{ width: 140 }}>
              <InputNumber min={5} max={600} />
            </Form.Item>
            <Form.Item name="max_concurrency" label="最大并发" style={{ width: 140 }}>
              <InputNumber min={1} max={64} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>

      <Modal
        title={modelModal.edit ? "编辑模型" : "新增模型"}
        open={modelModal.open}
        onCancel={() => setModelModal({ open: false })}
        onOk={() => modelForm.submit()}
        confirmLoading={saveModel.isPending}
        width={620}
      >
        <Form form={modelForm} layout="vertical" onFinish={(values) => saveModel.mutate(values)}>
          <Space style={{ display: "flex" }} align="start">
            <Form.Item name="model_name" label="模型名" rules={[{ required: true }]} style={{ width: 260 }}>
              <Input placeholder="deepseek-chat" />
            </Form.Item>
            <Form.Item name="display_name" label="显示名" rules={[{ required: true }]} style={{ width: 260 }}>
              <Input placeholder="DeepSeek Chat" />
            </Form.Item>
          </Space>
          <Space align="start">
            <Form.Item name="context_window" label="上下文窗口" style={{ width: 160 }}>
              <InputNumber min={1024} step={1024} />
            </Form.Item>
            <Form.Item name="max_output_tokens" label="最大输出" style={{ width: 160 }}>
              <InputNumber min={256} step={256} />
            </Form.Item>
          </Space>
          <Space align="start">
            <Form.Item name="cap_tools" label="工具调用" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="cap_json" label="JSON 输出" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="cap_vision" label="视觉" valuePropName="checked"><Switch /></Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
          </Space>
          <div style={{ color: "#6b7280", fontSize: 12 }}>
            <ApiOutlined /> 工具调用能力缺失时，引擎自动降级为提示词 JSON 协议。
          </div>
        </Form>
      </Modal>
    </div>
  );
}
