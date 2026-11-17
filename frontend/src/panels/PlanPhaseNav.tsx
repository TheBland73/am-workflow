import { Empty, Progress } from "antd";
import type { PlanPhase, PlanPhases } from "../api/types";

interface Props {
  phases: PlanPhases | undefined;
  loading: boolean;
  selectedUid: string | null;
  errorUids: Set<string>;
  onSelect: (uid: string) => void;
}

/** 左侧阶段导航：设计章节 → phase，phase 下挂任务；顶部显示章节承接进度。 */
export default function PlanPhaseNav({
  phases, loading, selectedUid, errorUids, onSelect,
}: Props) {
  const total = phases?.total_sections ?? 0;
  const covered = phases?.covered_sections ?? 0;
  const percent = total > 0 ? Math.round((covered / total) * 100) : 0;
  const groups: PlanPhase[] = phases?.phases ?? [];

  return (
    <aside className="plan-phase-nav">
      <div className="plan-phase-progress">
        <div className="plan-phase-progress-head">
          <b>任务进度 / PROGRESS</b>
          <em>{percent}%</em>
        </div>
        <Progress
          percent={percent}
          showInfo={false}
          size="small"
          strokeColor="#3b35c9"
          trailColor="#e7e5dc"
        />
        <span>
          {covered}/{total || "—"} 章节 · {phases?.total_tasks ?? 0} 任务 · {phases?.phase_count ?? 0} 阶段
        </span>
      </div>

      <div className="plan-phase-scroll">
        {loading ? (
          <div className="plan-phase-skeleton">
            {[0, 1, 2, 3].map((item) => <span key={item} />)}
          </div>
        ) : groups.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有任务" />
        ) : (
          groups.map((phase) => (
            <div className="plan-phase-group" key={`${phase.key}-${phase.index}`}>
              <div className="plan-phase-head">
                <b>
                  <i>{String(phase.index).padStart(2, "0")}</i>
                  {phase.title}
                </b>
                <em>{phase.task_count}</em>
              </div>
              <div className="plan-phase-tasks">
                {phase.tasks.map((task) => {
                  const active = selectedUid === task.uid;
                  return (
                    <button
                      key={task.uid}
                      type="button"
                      className={[
                        "plan-phase-task",
                        active ? "is-active" : "",
                        task.critical ? "is-critical" : "",
                        errorUids.has(task.uid) ? "is-error" : "",
                      ].join(" ").trim()}
                      title={task.title}
                      onClick={() => onSelect(task.uid)}
                    >
                      <span className="plan-phase-task-uid">{task.uid}</span>
                      <span className="plan-phase-task-title">{task.title}</span>
                      <span className="plan-phase-task-meta">
                        {task.role.toUpperCase()} · {task.effort}
                        {task.critical ? <i>关键</i> : null}
                      </span>
                    </button>
                  );
                })}
              </div>
            </div>
          ))
        )}
      </div>

      {phases && phases.uncovered.length > 0 ? (
        <div className="plan-phase-uncovered" title={phases.uncovered.join(" / ")}>
          <b>{phases.uncovered.length} 个章节未承接</b>
          <span>{phases.uncovered.slice(0, 2).join(" · ")}</span>
        </div>
      ) : null}
    </aside>
  );
}
