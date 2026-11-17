import { useEffect, useMemo, useRef, useState } from "react";
import { Button, Input, Popconfirm, Select, message } from "antd";
import { DeleteOutlined, PlusOutlined, SaveOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import type {
  PlanHeader, PlanTask, PlanValidationError, TaskEffort, TaskPriority, TaskRole,
} from "../api/types";

interface Props {
  plan: PlanHeader;
  tasks: PlanTask[];
  selected: PlanTask | null;
  errors: PlanValidationError[];
  onSaved: (task: PlanTask) => void;
  onCreated: (task: PlanTask) => void;
  onDeleted: (uid: string) => void;
  locked?: boolean;
}

const ROLES: Array<{ value: TaskRole; label: string }> = [
  { value: "dev", label: "开发 DEV" },
  { value: "test", label: "测试 TEST" },
  { value: "design", label: "设计 DESIGN" },
  { value: "pm", label: "计划 PM" },
  { value: "review", label: "评审 REVIEW" },
  { value: "ops", label: "运维 OPS" },
];

const EFFORTS: Array<{ value: TaskEffort; label: string }> = [
  { value: "S", label: "S · 1" },
  { value: "M", label: "M · 2" },
  { value: "L", label: "L · 3" },
  { value: "XL", label: "XL · 5" },
];

const PRIORITIES: Array<{ value: TaskPriority; label: string }> = [
  { value: "P0", label: "P0" },
  { value: "P1", label: "P1" },
  { value: "P2", label: "P2" },
];

interface Draft {
  title: string;
  description: string;
  deps: string[];
  design_section: string;
  acceptance: string;
  role_key: TaskRole;
  effort: TaskEffort;
  priority: TaskPriority;
}

function toDraft(task: PlanTask): Draft {
  return {
    title: task.title,
    description: task.description,
    deps: [...task.deps],
    design_section: task.design_section,
    acceptance: task.acceptance.join("\n"),
    role_key: task.role_key,
    effort: task.effort,
    priority: task.priority,
  };
}

export default function PlanInspector({
  plan, tasks, selected, errors, onSaved, onCreated, onDeleted, locked = false,
}: Props) {
  const [draft, setDraft] = useState<Draft | null>(selected ? toDraft(selected) : null);
  const [saving, setSaving] = useState(false);
  const [messageApi, holder] = message.useMessage();
  // 每个任务留一份未保存草稿：切走再切回来不丢，保存后清掉。
  const draftsRef = useRef(new Map<string, Draft>());

  useEffect(() => {
    if (!selected) {
      setDraft(null);
      return;
    }
    setDraft(draftsRef.current.get(selected.task_uid) ?? toDraft(selected));
  }, [selected?.task_uid]); // eslint-disable-line react-hooks/exhaustive-deps

  function edit(next: Draft) {
    setDraft(next);
    if (selected) draftsRef.current.set(selected.task_uid, next);
  }

  const dirty = Boolean(selected && draft
    && JSON.stringify(draft) !== JSON.stringify(toDraft(selected)));

  const sectionOptions = useMemo(() => {
    const titles = plan.sections.map((section) => ({
      value: section.title,
      label: `${section.line} · ${section.title}`,
    }));
    if (draft?.design_section && !titles.some((item) => item.value === draft.design_section)) {
      titles.unshift({ value: draft.design_section, label: draft.design_section });
    }
    return titles;
  }, [plan.sections, draft?.design_section]);

  const depsOptions = useMemo(
    () => tasks
      .filter((task) => task.task_uid !== selected?.task_uid)
      .map((task) => ({ value: task.task_uid, label: `${task.task_uid} · ${task.title}` })),
    [tasks, selected?.task_uid],
  );

  const selectedErrors = errors.filter((error) => error.task_uid === selected?.task_uid);

  async function save() {
    if (!selected || !draft) return;
    setSaving(true);
    try {
      const updated = await api.patchPlanTask(plan.id, selected.task_uid, {
        title: draft.title.trim(),
        description: draft.description.trim(),
        deps: draft.deps,
        design_section: draft.design_section,
        acceptance: draft.acceptance.split("\n").map((line) => line.trim()).filter(Boolean),
        role_key: draft.role_key,
        effort: draft.effort,
        priority: draft.priority,
      });
      messageApi.success("任务已保存，正在重新校验");
      draftsRef.current.delete(selected.task_uid);
      setDraft(toDraft(updated));
      onSaved(updated);
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function create() {
    setSaving(true);
    try {
      const created = await api.createPlanTask(plan.id, {
        title: "新任务",
        description: "补充任务描述与验收标准。",
        deps: [],
        design_section: plan.sections[0]?.title ?? "",
        acceptance: [],
        role_key: "dev",
        effort: "M",
        priority: "P1",
      });
      onCreated(created);
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!selected) return;
    setSaving(true);
    try {
      const result = await api.deletePlanTask(plan.id, selected.task_uid);
      draftsRef.current.delete(selected.task_uid);
      messageApi.success(result.removed_from.length
        ? `已删除，并从 ${result.removed_from.join("、")} 的依赖中移除`
        : "任务已删除");
      onDeleted(selected.task_uid);
    } catch (error) {
      messageApi.error((error as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (!selected || !draft) {
    return (
      <aside className="plan-inspector">
        {holder}
        <div className="plan-panel-head">
          <b>任务详情 / INSPECT</b>
          <Button size="small" icon={<PlusOutlined />} disabled={locked} onClick={create} loading={saving}>新增</Button>
        </div>
        <div className="plan-empty">
          <b>未选中任务</b>
          <span>在左侧图中点一个节点，或切到表格选择一行。</span>
        </div>
      </aside>
    );
  }

  return (
    <aside className="plan-inspector">
      {holder}
      <div className="plan-panel-head">
        <b>任务详情 / INSPECT</b>
        <span className="plan-kicker">{selected.task_uid}</span>
        {dirty ? <span className="plan-dirty">未保存</span> : null}
        {locked ? (
          <div className="plan-locked-note">计划已提交，任务详情只读；如需修改请在提交前操作。</div>
        ) : null}
      </div>

      {selectedErrors.length > 0 ? (
        <div className="plan-errors">
          {selectedErrors.map((error) => (
            <div key={`${error.code}-${error.message}`}>
              <b>{error.code}</b>
              <span>{error.message}</span>
            </div>
          ))}
        </div>
      ) : null}

      <label className="plan-field">
        <span>标题 · TITLE</span>
        <Input
          value={draft.title}
          disabled={locked}
          maxLength={40}
          onChange={(event) => edit({ ...draft, title: event.target.value })}
        />
      </label>

      <label className="plan-field">
        <span>描述 · DESCRIPTION</span>
        <Input.TextArea
          rows={4}
          value={draft.description}
          disabled={locked}
          onChange={(event) => edit({ ...draft, description: event.target.value })}
        />
      </label>

      <div className="plan-field-row">
        <label className="plan-field">
          <span>角色 · ROLE</span>
          <Select
            value={draft.role_key}
            disabled={locked}
            options={ROLES}
            onChange={(value) => edit({ ...draft, role_key: value })}
          />
        </label>
        <label className="plan-field">
          <span>工作量 · EFFORT</span>
          <Select
            value={draft.effort}
            disabled={locked}
            options={EFFORTS}
            onChange={(value) => edit({ ...draft, effort: value })}
          />
        </label>
      </div>

      <div className="plan-field-row">
        <label className="plan-field">
          <span>优先级 · PRIORITY</span>
          <Select
            value={draft.priority}
            disabled={locked}
            options={PRIORITIES}
            onChange={(value) => edit({ ...draft, priority: value })}
          />
        </label>
        <label className="plan-field">
          <span>设计章节 · SECTION</span>
          <Select
            showSearch
            value={draft.design_section || undefined}
            disabled={locked}
            options={sectionOptions}
            onChange={(value) => edit({ ...draft, design_section: value })}
            placeholder="选择章节"
          />
        </label>
      </div>

      <label className="plan-field">
        <span>依赖 · DEPENDS ON</span>
        <Select
          mode="multiple"
          allowClear
          value={draft.deps}
          disabled={locked}
          options={depsOptions}
          onChange={(value) => edit({ ...draft, deps: value })}
          placeholder="无依赖"
        />
      </label>

      <label className="plan-field">
        <span>验收 · ACCEPTANCE（每行一条）</span>
        <Input.TextArea
          rows={4}
          value={draft.acceptance}
          disabled={locked}
          onChange={(event) => edit({ ...draft, acceptance: event.target.value })}
        />
      </label>

      {locked ? null : (
      <div className="plan-actions">
        <Button type="primary" icon={<SaveOutlined />} loading={saving} onClick={save}>
          保存并校验
        </Button>
        {dirty ? (
          <Button
            onClick={() => {
              if (!selected) return;
              draftsRef.current.delete(selected.task_uid);
              setDraft(toDraft(selected));
            }}
          >
            放弃修改
          </Button>
        ) : null}
        <Button icon={<PlusOutlined />} onClick={create}>新增</Button>
        <Popconfirm
          title="删除这个任务？"
          description="其他任务对它的依赖会一并移除。"
          okText="删除"
          cancelText="取消"
          onConfirm={remove}
        >
          <Button danger icon={<DeleteOutlined />}>删除</Button>
        </Popconfirm>
      </div>
      )}
    </aside>
  );
}
