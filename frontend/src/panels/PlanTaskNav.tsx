import { useMemo } from "react";
import type { PlanSnapshot, PlanTask } from "../api/types";

interface Props {
  tasks: PlanTask[];
  snapshot: PlanSnapshot | null;
  selected: string | null;
  onSelect: (uid: string) => void;
  onInspect: () => void;
}

const EFFORT_LABEL: Record<string, string> = { S: "S", M: "M", L: "L", XL: "XL" };

export default function PlanTaskNav({ tasks, snapshot, selected, onSelect, onInspect }: Props) {
  const waves = useMemo(() => {
    const waveOf: Record<string, number> = {};
    Object.entries(snapshot?.waves ?? {}).forEach(([wave, members]) => {
      const number = Number(wave);
      (members ?? []).forEach((uid) => { waveOf[uid] = number; });
    });
    const groups: Record<number, PlanTask[]> = {};
    tasks.forEach((task) => {
      const wave = waveOf[task.task_uid] ?? 1;
      (groups[wave] ??= []).push(task);
    });
    return Object.entries(groups)
      .map(([wave, items]) => ({ wave: Number(wave), items }))
      .sort((a, b) => a.wave - b.wave);
  }, [tasks, snapshot]);

  const errorUids = useMemo(() => {
    const set = new Set<string>();
    snapshot?.validation?.errors?.forEach((error) => {
      if (error.task_uid) set.add(error.task_uid);
    });
    return set;
  }, [snapshot]);

  const validCount = tasks.length - errorUids.size;
  const invalid = snapshot?.validation?.ok === false;
  const percent = tasks.length ? Math.round((validCount / tasks.length) * 100) : 0;
  const percentText = invalid ? "ERR" : `${percent}%`;

  return (
    <aside className="plan-nav">
      <div className="plan-nav-head">
        <div className="plan-nav-title">
          <b>任务进度 / PROGRESS</b>
          <span>{percentText}</span>
        </div>
        <div className="plan-progress" aria-hidden="true">
          <span style={{ width: `${invalid ? 0 : percent}%` }} />
        </div>
        <div className="plan-nav-count">
          <b>{validCount}</b> / {tasks.length} TASKS
          <span>{waves.length} WAVES</span>
        </div>
        <button type="button" className="plan-nav-inspect" onClick={onInspect}>
          任务详情 / 新增
        </button>
      </div>

      <div className="plan-nav-body">
        {tasks.length === 0 ? (
          <div className="plan-nav-empty">
            <b>还没有任务</b>
            <span>生成任务后，阶段与任务会出现在这里。</span>
          </div>
        ) : waves.map(({ wave, items }) => (
          <section className="plan-phase" key={wave}>
            <div className="plan-phase-head">
              <b>WAVE {String(wave).padStart(2, "0")}</b>
              <span>{items.length} TASKS</span>
            </div>
            {items.map((task) => {
              const error = errorUids.has(task.task_uid);
              return (
                <button
                  key={task.task_uid}
                  type="button"
                  className={[
                    "plan-nav-task",
                    selected === task.task_uid ? "is-selected" : "",
                    error ? "is-error" : "",
                  ].join(" ").trim()}
                  onClick={() => onSelect(task.task_uid)}
                >
                  <span className={`plan-nav-dot${error ? " is-error" : ""}`} aria-hidden="true" />
                  <span className="plan-nav-text">
                    <b>{task.task_uid} · {task.title}</b>
                    <small>
                      {task.role_key.toUpperCase()} · {EFFORT_LABEL[task.effort] ?? task.effort} · {task.priority}
                    </small>
                  </span>
                </button>
              );
            })}
          </section>
        ))}
      </div>

      <div className="plan-nav-foot">
        <span><i className="plan-key is-dot" />校验通过</span>
        <span><i className="plan-key is-error" />需修正</span>
      </div>
    </aside>
  );
}
