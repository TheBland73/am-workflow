import { useMemo, useState } from "react";
import type { CSSProperties } from "react";
import {
  Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Popconfirm, Select, Space,
  Spin, Switch, Table, message,
} from "antd";
import {
  ApiOutlined, CloudDownloadOutlined, DeleteOutlined, EditOutlined, ExperimentOutlined,
  PlusOutlined, ThunderboltOutlined,
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
  const [remoteModal, setRemoteModal] = useState<{ open: boolean; providerId?: number }>({ open: false });
  const [presetKey, setPresetKey] = useState<string | undefined>();
  const [remoteProviderId, setRemoteProviderId] = useState<number | undefined>();
  const [picked, setPicked] = useState<string[]>([]);
  const [form] = Form.useForm();
  const [modelForm] = Form.useForm();

  const providers = useQuery({ queryKey: ["providers"], queryFn: api.providers });
  const models = useQuery({ queryKey: ["models"], queryFn: () => api.models() });
  const presets = useQuery({ queryKey: ["presets"], queryFn: api.presets });
  const remoteModels = useQuery({
    queryKey: ["remote-models", remoteProviderId],
    queryFn: () => api.remoteModels(remoteProviderId as number),
    enabled: !!remoteProviderId,
    retry: false,
    staleTime: 30_000,
  });

  const allModels = models.data?.items ?? [];
  const allProviders = providers.data?.items ?? [];
  const modelProvider = allProviders.find(
    (provider) => provider.id === (modelModal.providerId ?? modelModal.edit?.provider_id),
  );
  const canDiscover = modelProvider?.protocol === "openai_chat";
  const remoteProvider = allProviders.find((provider) => provider.id === remoteModal.providerId);
  const localNames = useMemo(
    () => new Set(allModels
      .filter((model) => model.provider_id === remoteModal.providerId)
      .map((model) => model.model_name)),
    [allModels, remoteModal.providerId],
  );

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
      if (!edit && presetKey) body.preset_key = presetKey;
      return edit ? api.patchProvider(edit.id, body) : api.createProvider(body);
    },
    onSuccess: (result) => {
      const seeded = (result as Provider).models_created ?? 0;
      if (seeded > 0) messageApi.success(`已按预设写入 ${seeded} 个模型`);
      setProviderModal({ open: false });
      setPresetKey(undefined);
      form.resetFields();
      refresh();
    },
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
    mutationFn: ({ providerId, modelId }: { providerId: number; modelId?: number }) =>
      api.testProvider(providerId, modelId),
    onSuccess: (result) => {
      const suffix = result.latency_ms ? `（${result.latency_ms} ms）` : "";
      messageApi[result.ok ? "success" : "error"](`${result.message}${suffix}`);
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const importModels = useMutation({
    mutationFn: (names: string[]) => api.importModels(remoteModal.providerId as number, names),
    onSuccess: (result) => {
      const skipped = result.skipped.length;
      messageApi.success(
        `已导入 ${result.created.length} 个模型${skipped ? `，跳过已存在的 ${skipped} 个` : ""}`,
      );
      setPicked([]);
      setRemoteModal({ open: false });
      refresh();
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const newRemoteIds = (remoteModels.data?.items ?? [])
    .filter((model) => !localNames.has(model.id))
    .map((model) => model.id);

  const openCreate = () => {
    setProviderModal({ open: true });
    setPresetKey(undefined);
    form.setFieldsValue({
      protocol: "openai_chat", timeout_s: 60, max_concurrency: 2, enabled: true, name: "",
      base_url: "", api_key: "",
    });
  };

  const openEdit = (provider: Provider) => {
    setProviderModal({ open: true, edit: provider });
    setPresetKey(undefined);
    form.setFieldsValue({
      name: provider.name, protocol: provider.protocol, base_url: provider.base_url,
      api_key: "", timeout_s: provider.timeout_s,
      max_concurrency: provider.max_concurrency, enabled: provider.enabled,
    });
  };

  const firstLocalModel = (providerId: number) =>
    allModels.find((model) => model.provider_id === providerId && model.enabled);

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
          dataSource={allProviders}
          pagination={false}
          expandable={{
            expandedRowRender: (provider) => {
              const rows = allModels.filter((model) => model.provider_id === provider.id);
              return (
                <div style={{ padding: "4px 12px 8px" }}>
                  <div style={{ marginBottom: 8, display: "flex", justifyContent: "space-between" }}>
                    <span style={{ color: "#6b7280", fontSize: 12 }}>该供应商下的模型</span>
                    <Space size={6}>
                      <Button
                        size="small"
                        icon={<CloudDownloadOutlined />}
                        disabled={provider.protocol !== "openai_chat"}
                        onClick={() => {
                          setPicked([]);
                          setRemoteProviderId(provider.id);
                          setRemoteModal({ open: true, providerId: provider.id });
                        }}
                      >
                        拉取模型
                      </Button>
                      <Button
                        size="small" icon={<PlusOutlined />}
                        onClick={() => {
                          setModelModal({ open: true, providerId: provider.id });
                          setRemoteProviderId(provider.protocol === "openai_chat" ? provider.id : undefined);
                          modelForm.setFieldsValue({
                            context_window: 32000, max_output_tokens: 4096,
                            cap_tools: true, cap_json: true, cap_vision: false, enabled: true,
                          });
                        }}
                      >
                        新增模型
                      </Button>
                    </Space>
                  </div>
                  <Table
                    rowKey="id" size="small" pagination={false} dataSource={rows}
                    locale={{
                      emptyText: (
                        <div style={{ padding: "10px 0" }}>
                          {rows.length === 0 && provider.protocol === "openai_chat"
                            ? <InlineDiscovery provider={provider} onImported={refresh} />
                            : <span style={{ color: "#9ca3af" }}>暂无数据</span>}
                        </div>
                      ),
                    }}
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
                        title: "", width: 130,
                        render: (_: unknown, model: ModelEntry) => (
                          <Space size={2}>
                            <Button
                              size="small" type="text" icon={<ExperimentOutlined />}
                              title="发一句测试，验证模型名与密钥"
                              loading={test.isPending && test.variables?.modelId === model.id}
                              onClick={() => test.mutate({ providerId: provider.id, modelId: model.id })}
                            />
                            <Button
                              size="small" type="text" icon={<EditOutlined />}
                              onClick={() => {
                                setModelModal({ open: true, edit: model });
                                setRemoteProviderId(provider.protocol === "openai_chat" ? provider.id : undefined);
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
              render: (_: unknown, provider: Provider) => {
                const local = firstLocalModel(provider.id);
                return (
                  <Space size={2}>
                    <Button
                      size="small" icon={<ThunderboltOutlined />}
                      loading={test.isPending && test.variables?.providerId === provider.id}
                      onClick={() => test.mutate({ providerId: provider.id, modelId: local?.id })}
                    >
                      测试
                    </Button>
                    <Button size="small" type="text" icon={<EditOutlined />} onClick={() => openEdit(provider)} />
                    <Popconfirm title="删除供应商及其模型？" onConfirm={() => removeProvider.mutate(provider.id)}>
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                    </Popconfirm>
                  </Space>
                );
              },
            },
          ]}
        />
      </div>

      <Modal
        title={providerModal.edit ? "编辑供应商" : "新增供应商"}
        open={providerModal.open}
        onCancel={() => { setProviderModal({ open: false }); setPresetKey(undefined); }}
        onOk={() => form.submit()}
        confirmLoading={saveProvider.isPending}
        width={640}
      >
        <Form
          form={form} layout="vertical"
          onFinish={(values) => saveProvider.mutate(values)}
        >
          {!providerModal.edit && (
            <Form.Item
              label="从预设填充"
              extra="选择预设会在创建供应商时自动写入其内置模型（仍需填 API Key）。"
            >
              <Select
                allowClear
                placeholder="选择预设（可再手改）"
                value={presetKey}
                options={(presets.data?.presets ?? []).map((preset) => ({
                  value: preset.key,
                  label: `${preset.name} · ${preset.protocol}`,
                }))}
                onChange={(key) => {
                  setPresetKey(key);
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
          {canDiscover && (
            <Form.Item
              label="从供应商读取"
              extra="自动拉取 GET /models 列表，选中后填入模型名（能力/上下文仍按需调整）。"
            >
              <Select
                showSearch
                allowClear
                loading={remoteModels.isFetching}
                placeholder="选择远程模型自动填入"
                options={(remoteModels.data?.items ?? []).map((model) => ({
                  value: model.id,
                  label: model.owned_by ? `${model.id} · ${model.owned_by}` : model.id,
                }))}
                notFoundContent={
                  remoteModels.isError
                    ? <span style={{ fontSize: 12 }}>{(remoteModels.error as Error).message}</span>
                    : remoteModels.isFetching ? "读取中…" : "没有可选项，手动填写即可"
                }
                onChange={(value: string) => {
                  const display = modelForm.getFieldValue("display_name");
                  modelForm.setFieldsValue({ model_name: value, display_name: display || value });
                }}
              />
            </Form.Item>
          )}
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

      <Modal
        title={`从 ${remoteProvider?.name ?? "供应商"} 拉取模型`}
        open={remoteModal.open}
        onCancel={() => setRemoteModal({ open: false })}
        width={640}
        footer={[
          <Button
            key="all"
            disabled={newRemoteIds.length === 0}
            onClick={() => setPicked(newRemoteIds)}
          >
            全选可导入（{newRemoteIds.length}）
          </Button>,
          <Button key="reload" onClick={() => remoteModels.refetch()} loading={remoteModels.isFetching}>
            重新拉取
          </Button>,
          <Button
            key="import" type="primary"
            disabled={picked.length === 0}
            loading={importModels.isPending}
            onClick={() => importModels.mutate(picked)}
          >
            导入选中（{picked.length}）
          </Button>,
        ]}
      >
        {remoteModels.isLoading ? (
          <div style={{ textAlign: "center", padding: 32 }}><Spin /></div>
        ) : remoteModels.isError ? (
          <Alert
            type="error" showIcon message="拉取失败"
            description={(
              <div>
                {(remoteModels.error as Error).message}
                <div style={{ marginTop: 8, fontSize: 12, color: "#6b7280" }}>
                  先确认后端已重启（拉取接口是新增的，旧进程会返回 “Not Found”）；
                  再检查 Base URL 是否以 <span className="am-mono">/v1</span> 结尾，例如
                  <span className="am-mono"> https://api.deepseek.com/v1</span>。
                </div>
              </div>
            )}
          />
        ) : (remoteModels.data?.items.length ?? 0) === 0 ? (
          <Alert type="info" showIcon message="供应商没有返回任何模型" />
        ) : (
          <div style={{ maxHeight: 320, overflowY: "auto", padding: "4px 2px" }}>
            <Checkbox.Group
              value={picked}
              onChange={(values) => setPicked(values as string[])}
              style={{ display: "flex", flexDirection: "column", gap: 8 }}
            >
              {(remoteModels.data?.items ?? []).map((model) => (
                <Checkbox key={model.id} value={model.id} disabled={localNames.has(model.id)}>
                  <span className="am-mono">{model.id}</span>
                  {model.owned_by ? <span style={{ color: "#6b7280" }}> · {model.owned_by}</span> : null}
                  {localNames.has(model.id) ? <span className="ef-tag" style={{ marginLeft: 6 }}>已存在</span> : null}
                </Checkbox>
              ))}
            </Checkbox.Group>
          </div>
        )}
        <div style={{ marginTop: 10, color: "#6b7280", fontSize: 12 }}>
          勾选后写入本地模型表；已存在的同名模型自动跳过，不会覆盖你改过的能力参数。
        </div>
      </Modal>
    </div>
  );
}

/** 供应商下没有模型时，自动读取远程列表并给出一键导入。 */
function InlineDiscovery({ provider, onImported }: { provider: Provider; onImported: () => void }) {
  const [messageApi, contextHolder] = message.useMessage();
  const remote = useQuery({
    queryKey: ["remote-models", provider.id],
    queryFn: () => api.remoteModels(provider.id),
    retry: false,
    staleTime: 60_000,
  });
  const importer = useMutation({
    mutationFn: (names: string[]) => api.importModels(provider.id, names),
    onSuccess: (result) => {
      messageApi.success(`已导入 ${result.created.length} 个模型`);
      onImported();
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const items = remote.data?.items ?? [];
  const rowStyle: CSSProperties = {
    display: "flex", alignItems: "center", gap: 10, fontSize: 13, color: "#4b5563",
    padding: "4px 0", textAlign: "left",
  };

  if (remote.isLoading) {
    return (
      <div style={rowStyle}>
        {contextHolder}
        <Spin size="small" /> 正在读取该供应商的模型列表…
      </div>
    );
  }
  if (remote.isError) {
    return (
      <div style={{ ...rowStyle, flexWrap: "wrap" }}>
        {contextHolder}
        <span style={{ color: "#b91c1c" }}>读取模型失败：{(remote.error as Error).message}</span>
        <Button size="small" onClick={() => remote.refetch()} loading={remote.isFetching}>重试</Button>
        <span style={{ color: "#9ca3af", fontSize: 12 }}>
          Base URL 需带 /v1；确认后端已重启后再试。
        </span>
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div style={{ ...rowStyle, color: "#9ca3af" }}>
        {contextHolder}
        供应商未返回任何模型，请点「新增模型」手动填写。
      </div>
    );
  }
  const names = items.slice(0, 6).map((model) => model.id).join("、");
  return (
    <div style={{ ...rowStyle, flexWrap: "wrap" }}>
      {contextHolder}
      <span>
        供应商提供 <b>{items.length}</b> 个模型：<span className="am-mono">{names}</span>
        {items.length > 6 ? " …" : ""}
      </span>
      <Button
        size="small" type="primary"
        loading={importer.isPending}
        onClick={() => importer.mutate(items.map((model) => model.id))}
      >
        全部导入
      </Button>
    </div>
  );
}
