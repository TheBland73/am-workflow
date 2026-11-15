import { useEffect, useRef, useState } from "react";
import { Empty, Segmented, Tag } from "antd";
import { useRunStore } from "../store/runStore";

type Filter = "all" | "warn" | "error";

export default function LogPanel() {
  const logs = useRunStore((state) => state.logs);
  const streams = useRunStore((state) => state.streams);
  const [filter, setFilter] = useState<Filter>("all");
  const boxRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const shown = filter === "all"
    ? logs
    : logs.filter((entry) => (filter === "warn" ? entry.level !== "info" : entry.level === "error"));

  useEffect(() => {
    if (autoScroll && boxRef.current) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight;
    }
  }, [shown.length, autoScroll]);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <Segmented
          size="small"
          value={filter}
          onChange={(value) => setFilter(value as Filter)}
          options={[
            { value: "all", label: `全部 ${logs.length}` },
            { value: "warn", label: "警告" },
            { value: "error", label: "错误" },
          ]}
        />
        <Segmented
          size="small"
          value={autoScroll ? "on" : "off"}
          onChange={(value) => setAutoScroll(value === "on")}
          options={[{ value: "on", label: "自动滚底" }, { value: "off", label: "固定" }]}
        />
      </div>
      {Object.keys(streams).length > 0 && (
        <div className="am-log" style={{ maxHeight: 120, overflow: "auto", marginBottom: 8 }}>
          {Object.entries(streams).map(([key, text]) => (
            <div key={key}>
              <Tag color="blue">{key}</Tag>
              <span>{text.slice(-600)}</span>
            </div>
          ))}
        </div>
      )}
      <div className="am-log" ref={boxRef} style={{ height: "calc(100vh - 330px)", minHeight: 260 }}>
        {shown.length === 0 && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无事件" />}
        {shown.map((entry) => (
          <div key={entry.id} className={`am-log-line ${entry.level}`}>
            <span className="am-log-ts">{new Date(entry.ts).toLocaleTimeString()}</span>
            {entry.nodeKey && <span className="am-log-node">{entry.nodeKey}</span>}
            <span>{entry.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
