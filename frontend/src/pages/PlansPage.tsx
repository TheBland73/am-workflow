import { useState } from "react";
import {
  Button, Empty, Form, Input, Modal, Segmented, Table, Upload, message,
} from "antd";
import { PlusOutlined, ReloadOutlined, UploadOutlined } from "@ant-design/icons";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { PlanListItem, PlanSourceType, PlanStatus } from "../api/types";

const STATUS_LABEL: Record<PlanStatus, string> = {
  draft: "草稿",
  generating: "生成中",
  generated: "已生成",
  validating: "校验中",
  valid: "有效",
  invalid: "无效",
  editing: "编辑中",
  committed: "已提交",
  archived: "已归档",
  failed: "生成失败",
};

interface FormValues {
  product_name: string;
  design_text?: string;
  source_ref?: string;
}

export default function PlansPage() {
  const navigate = useNavigate();
  const client = useQueryClient();
  const [open, setOpen] = useState(false);
  const [sourceType, setSourceType] = useState<PlanSourceType>("text");
  const [fileText, setFileText] = useState("");
  const [fileName, setFileName] = useState("");
  const [form] = Form.useForm<FormValues>();
  const [messageApi, holder] = message.useMessage();

  const plans = useQuery({ queryKey: ["plans"], queryFn: () => api.plans(), refetchInterval: 4000 });

  const create = useMutation({
    mutationFn: (values: FormValues) => api.createPlan({
      product_name: values.product_name.trim(),
      source_type: sourceType,
      source_ref: sourceType === "artifact" ? values.source_ref?.trim() : undefined,
      design_text: sourceType === "upload" ? fileText : values.design_text,
    }),
    onSuccess: (detail) => {
      setOpen(false);
      form.resetFields();
      setFileText("");
      setFileName("");
      client.invalidateQueries({ queryKey: ["plans"] });
      navigate(`/plans/${detail.plan.id}`);
    },
    onError: (error: Error) => messageApi.error(error.message),
  });

  const columns = [
    {
      title: "计划",
      dataIndex: "id",
      width: 132,
      render: (value: string) => <span className="am-mono">{value}</span>,
    },
    {
      title: "产品",
      dataIndex: "product_name",
      render: (value: string, row: PlanListItem) => (
        <div className="plan-title-cell">
          <b>{value}</b>
          <span>{row.metrics?.wave_count ? `${row.metrics.wave_count} 波次 · 关键路径 ${row.metrics.critical_path_tasks ?? "—"}` : `${row.task_count} 个任务`}</span>
        </div>
      ),
    },
    {
      title: "来源",
      dataIndex: "source_type",
      width: 110,
      render: (value: PlanSourceType) => (
        <span className="ef-tag">{value === "artifact" ? "工件" : value === "upload" ? "上传" : "粘贴"}</span>
      ),
    },
    {
      title: "状态",
      dataIndex: "status",
      width: 120,
      render: (value: PlanStatus) => (
        <span className={`status-pill is-${value}`}>{STATUS_LABEL[value] ?? value}</span>
      ),
    },
    { title: "任务", dataIndex: "task_count", width: 72, render: (value: number) => value || "—" },
    {
      title: "波次",
      width: 72,
      render: (_: unknown, row: PlanListItem) => row.metrics?.wave_count ?? "—",
    },
    {
      title: "关键路径",
      width: 90,
      render: (_: unknown, row: PlanListItem) => row.metrics?.critical_path_tasks ?? "—",
    },
    {
      title: "更新时间",
      dataIndex: "updated_at",
      width: 168,
      render: (value: string) => value?.replace("T", " ").slice(0, 19) ?? "—",
    },
    {
      title: "",
      width: 76,
      render: (_: unknown, row: PlanListItem) => (
        <Button size="small" onClick={() => navigate(`/plans/${row.id}`)}>打开</Button>
      ),
    },
  ];

  return (
    <div className="plans-page">
      {holder}
      <div className="am-page-head">
        <div>
          <h1 className="am-page-title">编排工作台</h1>
          <div className="am-page-sub">
            设计规格进入 PM 分解，程序校验依赖、波次与关键路径；人确认后才提交。
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <Button icon={<ReloadOutlined />} onClick={() => plans.refetch()}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            新建计划
          </Button>
        </div>
      </div>

      <div className="am-card" style={{ padding: 8 }}>
        <Table
          rowKey="id"
          size="middle"
          loading={plans.isLoading}
          dataSource={plans.data?.items ?? []}
          columns={columns}
          pagination={false}
          locale={{ emptyText: <Empty description="还没有计划。新建后粘贴设计规格，生成候选任务。" /> }}
        />
      </div>

      <Modal
        title="新建计划"
        open={open}
        onCancel={() => setOpen(false)}
        okText="创建计划"
        confirmLoading={create.isPending}
        onOk={() => form.submit()}
        width={720}
      >
        <Form form={form} layout="vertical" onFinish={(values) => create.mutate(values)}>
          <Form.Item
            name="product_name"
            label="产品 / 项目名"
            rules={[{ required: true, message: "写一个名字，用于标题与运行说明" }]}
          >
            <Input placeholder="例：客户反馈收集与归类" maxLength={80} />
          </Form.Item>

          <div className="plan-source-picker">
            <span>设计来源</span>
            <Segmented
              value={sourceType}
              onChange={(value) => setSourceType(value as PlanSourceType)}
              options={[
                { label: "粘贴文本", value: "text" },
                { label: "上传 Markdown", value: "upload" },
                { label: "从工件导入", value: "artifact" },
              ]}
            />
          </div>

          {sourceType === "text" ? (
            <Form.Item
              name="design_text"
              label="设计规格"
              rules={[{ required: true, message: "至少 200 字，并包含 Markdown 章节" }]}
            >
              <Input.TextArea
                rows={12}
                placeholder={"# 产品名 · 设计规格\n\n## 1 目标与范围\n…\n\n## 2 数据模型\n…"}
              />
            </Form.Item>
          ) : null}

          {sourceType === "upload" ? (
            <div className="plan-upload">
              <Upload
                accept=".md,.txt"
                showUploadList={false}
                beforeUpload={async (file) => {
                  const text = await file.text();
                  setFileText(text);
                  setFileName(file.name);
                  if (!form.getFieldValue("product_name")) {
                    form.setFieldValue("product_name", file.name.replace(/\.(md|txt)$/i, ""));
                  }
                  return false;
                }}
              >
                <Button icon={<UploadOutlined />}>选择 Markdown 文件</Button>
              </Upload>
              <span>{fileName || "未选择文件"}</span>
              {fileText ? <small>{fileText.length} 字符 · 本地读取，创建时才发送</small> : null}
            </div>
          ) : null}

          {sourceType === "artifact" ? (
            <Form.Item
              name="source_ref"
              label="工件引用"
              rules={[{ required: true, message: "填 run_id:kind，例如 33155818b2a5:design" }]}
            >
              <Input placeholder="33155818b2a5:design" />
            </Form.Item>
          ) : null}
        </Form>
      </Modal>
    </div>
  );
}
