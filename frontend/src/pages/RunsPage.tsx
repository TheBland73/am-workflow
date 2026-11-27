import { useEffect, useMemo, useRef, useState } from "react";
import {
  Alert, Button, Collapse, Divider, Empty, Form, Input, InputNumber, Modal, Progress, Radio,
  Select, Space, Switch, Table, message,
} from "antd";
import { DeleteOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type {
  MaterialIn, ParamSpec, Run, RunConfig, RunStatus, TemplateDefinition,
} from "../api/types";
import { StatusTag, runStatusLabel } from "../components/RunStatusTag";
import {
  CHECKPOINT_DECIDERS, CHECKPOINT_LABEL, canJoinDiscussion, deciderLabel,
} from "../panels/meetingLabels";

export { StatusTag, runStatusLabel } from "../components/RunStatusTag";

const PHASE_LABELS: Record<string, string> = {
  idea: "想法",
  discussion: "讨论",
  proposal: "方案",
  review: "多方评价",
  requirement: "需求",
  design: "设计",
  tasks: "任务分解",
  execution: "执行",
  delivery: "交付",
};

const LEVEL_TEXT: Record<string, string> = {
  L1: "全程人审：每个确认点都停下来等人。",
  L2: "关键点人审（默认）：关键确认点等人，其余交给程序或模型。",
  L3: "仅兜底：只在失败或轮次用尽时找人。",
};
const LEVEL_NAME: Record<string, string> = {
  L1: "全程人审", L2: "关键点人审（默认）", L3: "仅兜底",
};

const PARAM_GROUP_LABEL: Record<string, string> = {
  discussion: "讨论", review: "评审", confirm: "需求确认", gates: "确认点",
  context: "上下文", meeting: "会议",
};

/** 在「常用参数」里单列的项。 */
const COMMON_PARAMS: Array<[string, string]> = [
  ["discussion", "rounds_max"],
  ["discussion", "closure_rule"],
  ["discussion", "proxy_enabled"],
];

type MaterialDraft = { name?: string; content?: string };

type CreateValues = {
  idea: string;
  budget?: number;
  level?: string;
  rosterMode?: string;
  rosterCount?: number;
  rosterRoles?: string[];
  materials?: MaterialDraft[];
  [key: string]: unknown;
};

function paramValues(definition: TemplateDefinition | undefined): Record<string, unknown> {
  const values: Record<string, unknown> = {};
  for (const [group, specs] of Object.entries(definition?.params ?? {})) {
    for (const [key, spec] of Object.entries(specs)) {
      values[`p.${group}.${key}`] = spec.default;
    }
  }
  return values;
}

function paramField(spec: ParamSpec) {
  if (spec.type === "bool") return <Switch />;
  if (spec.choices?.length) {
    return <Select options={spec.choices.map((value) => ({ value, label: value }))} />;
  }
  if (spec.type === "int" || spec.type === "float") {
    return (
      <InputNumber
        style={{ width: 160 }}
        min={spec.min}
        max={spec.max}
        step={spec.type === "float" ? 0.05 : 1}
      />
    );
  }
  return <Input />;
}

function paramItem(name: string, spec: ParamSpec) {
  const hint = [spec.recommend ? `建议 ${spec.recommend}` : "", spec.hint ?? ""]
    .filter(Boolean)
    .join(" · ");
  return (
    <Form.Item
      key={name}
      name={name}
      label={`${spec.title}（${spec.default}）`}
      extra={hint || undefined}
      valuePropName={spec.type === "bool" ? "checked" : "value"}
    >
      {paramField(spec)}
    </Form.Item>
  );
}

export default function RunsPage() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [createErrors, setCreateErrors] = useState<string[]>([]);
  const [form] = Form.useForm<CreateValues>();
  const [messageApi, contextHolder] = message.useMessage();
  // 默认值只在一次"新建"开始时写一遍：模板定义加载完成前用户先点的等级不被覆盖
  const initialized = useRef(false);

  const runs = useQuery({ queryKey: ["runs"], queryFn: api.listRuns, refetchInterval: 4000 });

  // 表单里的数值与选项全部来自模板定义，不在前端写死
  const definition = useQuery({
    queryKey: ["template-definition", "sdlc_v1"],
    queryFn: () => api.templateDefinition("sdlc_v1"),
    enabled: open,
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
  const roles = useQuery({
    queryKey: ["templates"],
    queryFn: api.templates,
    enabled: open,
    staleTime: 60_000,
  });

  useEffect(() => {
    if (!open) {
      initialized.current = false;
      return;
    }
    if (initialized.current || !definition.data) return;
    form.setFieldsValue({
      budget: 1200000,
      level: "L2",
      rosterMode: "none",
      ...paramValues(definition.data),
    });
    initialized.current = true;
  }, [open, definition.data, form]);

  const materials = Form.useWatch("materials", form) ?? [];
  const usedChars = materials.reduce((sum, item) => sum + (item?.content?.length ?? 0), 0);
  const level = Form.useWatch("level", form) ?? "L2";
  const rosterMode = Form.useWatch("rosterMode", form) ?? "none";

  const rosterRoleOptions = useMemo(
    () => (roles.data?.items ?? [])
      .filter(canJoinDiscussion)
      .map((item) => ({ value: item.role_key, label: `${item.name}（${item.role_key}）` })),
    [roles.data],
  );

  const create = useMutation({
    mutationFn: (values: CreateValues) => api.createRun(
      values.idea,
      "sdlc_v1",
      values.budget,
      buildConfig(values, definition.data),
      buildMaterials(values),
    ),
    onSuccess: (snapshot) => {
      setOpen(false);
      setCreateErrors([]);
      form.resetFields();
      client.invalidateQueries({ queryKey: ["runs"] });
      navigate(`/runs/${snapshot.run.id}`);
    },
    onError: (error: Error) => {
      // 后端把多项校验错误用分号连成一句放在 detail 里
      const detail = error.message.replace(/^运行级配置不合法：/, "");
      setCreateErrors(detail.split("；").map((item) => item.trim()).filter(Boolean));
    },
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

  const def = definition.data;
  const rosterMin = def?.params.discussion?.participants_min;
  const rosterMax = def?.params.discussion?.participants_max;
  const moreGroups = Object.entries(def?.params ?? {}).map(([group, specs]) => ({
    group,
    items: Object.entries(specs).filter(([key]) =>
      !COMMON_PARAMS.some(([hitGroup, hitKey]) => hitGroup === group && hitKey === key)),
  })).filter((item) => item.items.length > 0);

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
          <Button type="primary" icon={<PlusOutlined />} onClick={() => {
            form.resetFields();
            setCreateErrors([]);
            setOpen(true);
          }}>
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
        width={860}
      >
        {createErrors.length > 0 && (
          <Alert
            type="error"
            showIcon
            style={{ marginBottom: 12 }}
            message="配置不合法，未创建运行"
            description={(
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {createErrors.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
              </ul>
            )}
          />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => { setCreateErrors([]); create.mutate(values); }}
        >
          <Form.Item
            name="idea"
            label="想法"
            rules={[{ required: true, min: 10, message: "请至少写 10 个字，让讨论有输入" }]}
          >
            <Input.TextArea
              rows={5}
              placeholder="例：做一个面向小团队的「客户反馈收集与归类」内部工具：成员提交反馈，系统按主题聚类并给出优先级建议，支持导出周报。"
            />
          </Form.Item>
          <Form.Item name="budget" label="Token 预算">
            <InputNumber min={10000} step={100000} style={{ width: 220 }} />
          </Form.Item>

          <Form.Item name="level" label="自动化等级">
            <Radio.Group disabled={!def}>
              {Object.keys(def?.automation_levels ?? { L1: {}, L2: {}, L3: {} }).map((key) => (
                <Radio key={key} value={key}>
                  <div>{LEVEL_NAME[key] ?? key}</div>
                  <div style={{ color: "var(--ef-ink-2)", fontSize: 12 }}>
                    {LEVEL_TEXT[key] ?? ""}
                  </div>
                </Radio>
              ))}
            </Radio.Group>
          </Form.Item>
          {def?.automation_levels?.[level] && (
            <div style={{ fontSize: 12, color: "var(--ef-ink-2)", margin: "-8px 0 12px" }}>
              {Object.entries(def.automation_levels[level])
                .map(([checkpoint, decider]) =>
                  `${CHECKPOINT_LABEL[checkpoint] ?? checkpoint}：${deciderLabel(decider)}`)
                .join("；")}
            </div>
          )}

          <Collapse
            ghost
            items={[{
              key: "advanced",
              label: "高级设置：名单、参数、逐确认点覆盖、背景材料",
              children: (
                <div>
                  <Divider orientation="left" plain>参会名单</Divider>
                  <Form.Item name="rosterMode" label="名单怎么定">
                    <Radio.Group>
                      <Radio value="none">不指定（主持人在人数范围内定）</Radio>
                      <Radio value="count">固定人数</Radio>
                      <Radio value="roles">点名角色</Radio>
                    </Radio.Group>
                  </Form.Item>
                  {rosterMode === "count" && (
                    <Form.Item
                      name="rosterCount"
                      label="固定人数"
                      extra={rosterMin && rosterMax
                        ? `可填 ${rosterMin.min ?? 1} 到 ${rosterMax.max ?? 99} 人`
                        : undefined}
                    >
                      <InputNumber
                        style={{ width: 160 }}
                        min={rosterMin?.min ?? 1}
                        max={rosterMax?.max ?? 99}
                      />
                    </Form.Item>
                  )}
                  {rosterMode === "roles" && (
                    <Form.Item
                      name="rosterRoles"
                      label="参会角色"
                      extra="只列出适合参加讨论的角色；主持人、需求方代理不在此列。"
                    >
                      <Select
                        mode="multiple"
                        options={rosterRoleOptions}
                        loading={roles.isLoading}
                        placeholder="选择具体角色"
                      />
                    </Form.Item>
                  )}

                  <Divider orientation="left" plain>常用参数</Divider>
                  {COMMON_PARAMS.map(([group, key]) => {
                    const spec = def?.params[group]?.[key];
                    return spec ? paramItem(`p.${group}.${key}`, spec) : null;
                  })}

                  <Divider orientation="left" plain>逐确认点覆盖</Divider>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                    {Object.entries(CHECKPOINT_DECIDERS).map(([checkpoint, deciders]) => (
                      <Form.Item
                        key={checkpoint}
                        name={`cp.${checkpoint}`}
                        label={CHECKPOINT_LABEL[checkpoint] ?? checkpoint}
                        style={{ width: 180 }}
                      >
                        <Select
                          allowClear
                          placeholder="跟随等级"
                          options={deciders.map((value) => ({
                            value, label: deciderLabel(value),
                          }))}
                        />
                      </Form.Item>
                    ))}
                  </div>

                  <Divider orientation="left" plain>更多参数</Divider>
                  {moreGroups.map(({ group, items }) => (
                    <div key={group}>
                      <div style={{ fontSize: 12, color: "var(--ef-ink-2)", margin: "6px 0" }}>
                        {PARAM_GROUP_LABEL[group] ?? group}
                      </div>
                      <div style={{ display: "flex", flexWrap: "wrap", gap: 12 }}>
                        {items.map(([key, spec]) => (
                          <div key={`${group}.${key}`} style={{ width: 260 }}>
                            {paramItem(`p.${group}.${key}`, spec)}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}

                  <Divider orientation="left" plain>背景材料</Divider>
                  <Form.List name="materials">
                    {(fields, { add, remove }) => (
                      <div>
                        {fields.map((field) => (
                          <div key={field.key} style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
                            <Form.Item name={[field.name, "name"]} style={{ width: 180 }}>
                              <Input placeholder="材料名（可留空）" />
                            </Form.Item>
                            <Form.Item name={[field.name, "content"]} style={{ flex: 1 }}>
                              <Input.TextArea rows={3} placeholder="粘贴背景材料" />
                            </Form.Item>
                            <Button
                              type="text"
                              icon={<DeleteOutlined />}
                              onClick={() => remove(field.name)}
                            />
                          </div>
                        ))}
                        <Space>
                          <Button size="small" onClick={() => add({ name: "", content: "" })}>
                            添加材料
                          </Button>
                          <span style={{ fontSize: 12, color: "var(--ef-ink-2)" }}>
                            已用 {usedChars} 字；上限由后端校验，超出时提交会被拒绝并给出上限。
                          </span>
                        </Space>
                      </div>
                    )}
                  </Form.List>
                </div>
              ),
            }]}
          />
        </Form>
      </Modal>
    </div>
  );
}

function buildConfig(values: CreateValues, definition: TemplateDefinition | undefined): RunConfig {
  const config: RunConfig = {};
  const checkpoints: Record<string, string> = {};
  for (const checkpoint of Object.keys(CHECKPOINT_DECIDERS)) {
    const value = values[`cp.${checkpoint}`];
    if (typeof value === "string" && value) checkpoints[checkpoint] = value;
  }
  config.automation = {
    level: values.level ?? "L2",
    ...(Object.keys(checkpoints).length ? { checkpoints } : {}),
  };

  const params: Record<string, Record<string, unknown>> = {};
  for (const [group, specs] of Object.entries(definition?.params ?? {})) {
    for (const key of Object.keys(specs)) {
      const value = values[`p.${group}.${key}`];
      if (value !== undefined && value !== null && value !== "") {
        params[group] = { ...(params[group] ?? {}), [key]: value };
      }
    }
  }
  if (Object.keys(params).length) config.params = params;

  // 固定人数要把人数范围同步成同一个值：名单替换按 params 的 min/max 校验，
  // 不合并的话"固定 1 人"的运行一改名单就会被 3–5 的默认范围拒绝。
  if (values.rosterMode === "count") {
    const count = Number(values.rosterCount);
    if (count > 0) {
      config.params = {
        ...(config.params ?? {}),
        discussion: {
          ...(config.params?.discussion ?? {}),
          participants_min: count,
          participants_max: count,
        },
      };
    }
  }

  if (values.rosterMode === "count" && values.rosterCount) {
    config.rosters = { participants: { count: Number(values.rosterCount) } };
  } else if (values.rosterMode === "roles" && (values.rosterRoles?.length ?? 0) > 0) {
    config.rosters = { participants: { role_keys: values.rosterRoles } };
  }
  return config;
}

function buildMaterials(values: CreateValues): MaterialIn[] {
  return (values.materials ?? [])
    .map((item) => ({
      name: (item?.name ?? "").trim(),
      content: (item?.content ?? "").trim(),
    }))
    .filter((item) => item.content)
    .map((item) => ({
      ...(item.name ? { name: item.name } : {}),
      content: item.content,
    }));
}
