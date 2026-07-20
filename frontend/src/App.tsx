import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";

type Organization = { id: string; name: string };
type Dataset = {
  id: string;
  name: string;
  description?: string | null;
  status?: string;
  created_at: string;
  updated_at: string;
};
type RemovedDataset = Dataset & {
  deleted_at: string;
  estimated_bytes: number;
  sample_count: number;
  batch_count: number;
  source_upload_count: number;
};
type Upload = {
  id: string;
  import_batch_id: string;
  status: string;
  upload_url?: string;
  preview?: {
    image_count: number;
    annotation_count: number;
    parser_name?: string;
  } | null;
};
type Batch = {
  id: string;
  batch_number: number;
  batch_name: string;
  source_type: string;
  status: string;
  note?: string | null;
  created_at: string;
};
type Sample = {
  id: string;
  file_name: string;
  relative_path: string;
  subset?: string | null;
  annotation_type?: string | null;
  width?: number | null;
  height?: number | null;
  import_batch_id: string;
};
type Page<T> = { items: T[]; total: number; limit: number; offset: number };
type Stats = {
  sample_count: number;
  annotated_sample_count?: number;
  missing_annotation_count?: number;
  by_subset: Record<string, number>;
  by_annotation_type: Record<string, number>;
  class_distribution?: {
    class_id: number;
    class_name: string;
    sample_count: number;
  }[];
};
type Label = {
  id?: string;
  class_id: number;
  class_name: string;
  color?: string | null;
};
type Issue = {
  id: string;
  issue_type: string;
  severity: string;
  detail_code: string;
};
type History = {
  id: string;
  action: string;
  summary: string;
  status: string;
  created_at: string;
};
type Annotation = {
  class_id: number;
  class_name: string;
  bbox?: number[] | null;
  polygon?: number[][];
  keypoints?: number[][];
  coordinate_space: string;
};
type Preview = {
  sample: Sample;
  image_url?: string | null;
  annotation_url?: string | null;
  normalized_annotation_url?: string | null;
  summary?: {
    annotation_count: number;
    bbox_count: number;
    polygon_count: number;
  } | null;
};
type PurgePreview = {
  dataset_id: string;
  dataset_name: string;
  sample_count: number;
  version_count: number;
  batch_count: number;
  source_upload_count: number;
  object_count: number;
  estimated_bytes: number;
  stale_temp_directory_count: number;
  stale_temp_bytes: number;
};
type Normalized = {
  image: { width?: number | null; height?: number | null };
  annotations: Annotation[];
};
type ModalName =
  | "create"
  | "upload"
  | "export"
  | "labels"
  | "history"
  | "batch"
  | "delete-dataset"
  | "storage"
  | "purge-dataset"
  | null;

const colors = ["#18b7a0", "#f2aa4c", "#6387d8", "#df6b77", "#a477c9"];
const formatBytes = (value: number) =>
  value < 1024
    ? `${value} B`
    : value < 1024 ** 2
      ? `${(value / 1024).toFixed(1)} KiB`
      : value < 1024 ** 3
        ? `${(value / 1024 ** 2).toFixed(1)} MiB`
        : `${(value / 1024 ** 3).toFixed(2)} GiB`;
let authExpiredHandler: (() => void) | null = null;
const AUTH_ERRORS = new Set([
  "auth.invalid_token",
  "auth.required",
  "auth.invalid_user",
]);
const batchStatusLabels: Record<string, string> = {
  uploading: "上传中",
  queued: "等待处理",
  scanning: "正在扫描",
  waiting_confirmation: "等待确认",
  importing: "正在导入",
  ready: "已完成",
  import_failed: "导入失败",
  deleted: "已删除",
};
const batchStatusLabel = (status: string) =>
  batchStatusLabels[status] ?? status.replaceAll("_", " ");
const api = async <T,>(
  path: string,
  token?: string,
  init?: RequestInit,
): Promise<T> => {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body?.error?.code ?? "request.failed";
    if (response.status === 401 && AUTH_ERRORS.has(detail)) {
      localStorage.removeItem("dataset-platform-token");
      authExpiredHandler?.();
      throw new Error("登录已过期，请重新登录");
    }
    throw new Error(detail);
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
};
function Modal({
  title,
  onClose,
  children,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="modal-backdrop" onMouseDown={onClose}>
      <section
        className={`modal ${wide ? "wide" : ""}`}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <p className="kicker">DATASET WORKBENCH</p>
            <h2>{title}</h2>
          </div>
          <button className="icon-button" onClick={onClose}>
            ×
          </button>
        </header>
        {children}
      </section>
    </div>
  );
}
function Overlay({ payload }: { payload: Normalized }) {
  const width = payload.image.width || 1,
    height = payload.image.height || 1;
  const point = (value: number[], space: string) =>
    space === "normalized" ? [value[0] * width, value[1] * height] : value;
  return (
    <svg
      className="annotation-overlay"
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
    >
      {payload.annotations.map((item, index) => {
        const color = colors[Math.abs(item.class_id) % colors.length],
          box = item.bbox
            ? item.coordinate_space === "normalized"
              ? [
                  (item.bbox[0] - item.bbox[2] / 2) * width,
                  (item.bbox[1] - item.bbox[3] / 2) * height,
                  item.bbox[2] * width,
                  item.bbox[3] * height,
                ]
              : item.bbox
            : null,
          polygon = item.polygon
            ?.map((p) => point(p, item.coordinate_space).join(","))
            .join(" ");
        return (
          <g
            key={`${item.class_id}-${index}`}
            stroke={color}
            fill="none"
            strokeWidth={Math.max(width, height) / 360}
          >
            {box && (
              <rect x={box[0]} y={box[1]} width={box[2]} height={box[3]} />
            )}
            {polygon && <polygon points={polygon} fill={`${color}35`} />}
            {item.keypoints?.map((p, i) => {
              const [x, y] = point(p, item.coordinate_space);
              return (
                <circle
                  key={i}
                  cx={x}
                  cy={y}
                  r={Math.max(width, height) / 170}
                  fill={color}
                />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

export default function App() {
  const [token, setToken] = useState(
    localStorage.getItem("dataset-platform-token") ?? "",
  );
  const [email, setEmail] = useState("admin@example.local"),
    [password, setPassword] = useState("");
  const [org, setOrg] = useState<Organization | null>(null),
    [datasets, setDatasets] = useState<Dataset[]>([]),
    [dataset, setDataset] = useState<Dataset | null>(null);
  const [batches, setBatches] = useState<Batch[]>([]),
    [uploads, setUploads] = useState<Upload[]>([]),
    [samples, setSamples] = useState<Sample[]>([]),
    [stats, setStats] = useState<Stats | null>(null),
    [labels, setLabels] = useState<Label[]>([]),
    [issues, setIssues] = useState<Issue[]>([]),
    [history, setHistory] = useState<History[]>([]),
    [removedDatasets, setRemovedDatasets] = useState<RemovedDataset[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null),
    [normalized, setNormalized] = useState<Normalized | null>(null),
    [sampleTotal, setSampleTotal] = useState(0);
  const [view, setView] = useState<"workspace" | "trash">("workspace");
  const [batchId, setBatchId] = useState(""),
    [selectedIds, setSelectedIds] = useState<string[]>([]),
    [modal, setModal] = useState<ModalName>(null),
    [notice, setNotice] = useState(""),
    [showStats, setShowStats] = useState(false);
  const [filters, setFilters] = useState({
    name: "",
    subset: "",
    format: "",
    classId: "",
    annotation: "",
  });
  const [newName, setNewName] = useState(""),
    [file, setFile] = useState<File | null>(null),
    [batchName, setBatchName] = useState(""),
    [uploadProgress, setUploadProgress] = useState<number | null>(null),
    [exportFormat, setExportFormat] = useState("coco"),
    [exportSubsets, setExportSubsets] = useState<string[]>([]),
    [includeUnannotated, setIncludeUnannotated] = useState(true),
    [editableLabels, setEditableLabels] = useState<Label[]>([]),
    [editableBatch, setEditableBatch] = useState<Batch | null>(null),
    [datasetToDelete, setDatasetToDelete] = useState<Dataset | null>(null),
    [purgePreview, setPurgePreview] = useState<PurgePreview | null>(null),
    [purgeConfirmation, setPurgeConfirmation] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const samplesScrollRef = useRef<HTMLDivElement>(null);
  const pollingRef = useRef(false);
  const samplesLoadingRef = useRef(false);
  const sampleRequestRef = useRef(0);
  const notify = (message: string) => {
    setNotice(message);
    window.setTimeout(
      () => setNotice((current) => (current === message ? "" : current)),
      4500,
    );
  };
  const fail = (error: unknown) =>
    notify(error instanceof Error ? error.message : "操作未完成，请稍后重试。");
  const selectedBatch = batches.find((item) => item.id === batchId);

  const reloadShell = async () => {
    if (!token) return;
    const organizations = await api<Organization[]>("/organizations", token);
    const activeOrg = organizations[0] ?? null;
    setOrg(activeOrg);
    if (!activeOrg) return;
    const [data, removed] = await Promise.all([
      api<Page<Dataset>>(
        `/datasets?organization_id=${activeOrg.id}&limit=100`,
        token,
      ),
      api<Page<RemovedDataset>>(
        `/datasets/removed?organization_id=${activeOrg.id}&limit=100`,
        token,
      ),
    ]);
    setDatasets(data.items);
    setRemovedDatasets(removed.items);
    setDataset((current) =>
      current && data.items.some((item) => item.id === current.id)
        ? current
        : (data.items[0] ?? null),
    );
  };
  const reloadWorkspace = async () => {
    if (!token || !org || !dataset || view !== "workspace") return;
    const [
      partData,
      uploadData,
      nextStats,
      nextLabels,
      issueData,
      historyData,
    ] = await Promise.all([
      api<Page<Batch>>(
        `/datasets/${dataset.id}/import-batches?organization_id=${org.id}&limit=100`,
        token,
      ),
      api<Page<Upload>>(
        `/datasets/${dataset.id}/upload-sessions?organization_id=${org.id}`,
        token,
      ),
      api<Stats>(
        `/datasets/${dataset.id}/statistics?organization_id=${org.id}`,
        token,
      ),
      api<Label[]>(
        `/datasets/${dataset.id}/labels?organization_id=${org.id}`,
        token,
      ),
      api<Page<Issue>>(
        `/datasets/${dataset.id}/quality-issues?organization_id=${org.id}&limit=6`,
        token,
      ),
      api<Page<History>>(
        `/operation-history?organization_id=${org.id}&dataset_id=${dataset.id}&limit=30`,
        token,
      ),
    ]);
    setBatches(partData.items);
    setUploads(uploadData.items);
    setStats(nextStats);
    setLabels(nextLabels);
    setIssues(issueData.items);
    setHistory(historyData.items);
  };
  const loadSamplePage = async (nextOffset: number, append: boolean) => {
    if (!token || !org || !dataset || view !== "workspace") return;
    const requestId = ++sampleRequestRef.current;
    const q = new URLSearchParams({
      organization_id: org.id,
      limit: "100",
      offset: String(nextOffset),
    });
    if (filters.name) q.set("file_name", filters.name);
    if (filters.subset) q.set("subset", filters.subset);
    if (filters.format) q.set("annotation_type", filters.format);
    if (filters.classId) q.set("class_id", filters.classId);
    if (batchId) q.set("import_batch_id", batchId);
    if (filters.annotation) q.set("has_annotation", filters.annotation);
    const sampleData = await api<Page<Sample>>(
      `/datasets/${dataset.id}/samples?${q}`,
      token,
    );
    if (requestId !== sampleRequestRef.current) return;
    setSamples((current) =>
      append
        ? [
            ...current,
            ...sampleData.items.filter(
              (item) => !current.some((loaded) => loaded.id === item.id),
            ),
          ]
        : sampleData.items,
    );
    setSampleTotal(sampleData.total);
  };
  const loadMoreSamples = async () => {
    if (samplesLoadingRef.current || samples.length >= sampleTotal) return;
    samplesLoadingRef.current = true;
    try {
      await loadSamplePage(samples.length, true);
    } finally {
      samplesLoadingRef.current = false;
    }
  };
  useEffect(() => {
    authExpiredHandler = () => setToken("");
    return () => {
      authExpiredHandler = null;
    };
  }, []);
  useEffect(() => {
    void reloadShell().catch(fail);
  }, [token]);
  useEffect(() => {
    setSamples([]);
    setSampleTotal(0);
    setSelectedIds([]);
    setPreview(null);
    setNormalized(null);
    samplesScrollRef.current?.scrollTo({ top: 0 });
    void loadSamplePage(0, false).catch(fail);
  }, [
    token,
    org?.id,
    dataset?.id,
    view,
    batchId,
    filters.name,
    filters.subset,
    filters.format,
    filters.classId,
    filters.annotation,
  ]);
  useEffect(() => {
    void reloadWorkspace().catch(fail);
  }, [
    token,
    org?.id,
    dataset?.id,
    view,
    batchId,
    filters.name,
    filters.subset,
    filters.format,
    filters.classId,
    filters.annotation,
  ]);
  useEffect(() => {
    if (!token) return;
    let disposed = false;
    const poll = async () => {
      // Statistics/history queries are intentionally skipped during a direct upload.
      // They otherwise contend with the browser's large request and can pile up when
      // a slow WSL service takes longer than the old five-second interval.
      if (disposed || pollingRef.current || uploadProgress !== null) return;
      pollingRef.current = true;
      try {
        await Promise.all([reloadShell(), reloadWorkspace()]);
      } catch (error) {
        if (!disposed) fail(error);
      } finally {
        pollingRef.current = false;
      }
    };
    const timer = window.setInterval(() => void poll(), 15000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [
    token,
    org?.id,
    dataset?.id,
    view,
    batchId,
    filters.name,
    filters.subset,
    filters.format,
    filters.classId,
    filters.annotation,
    uploadProgress,
  ]);

  const signIn = async (event: FormEvent) => {
    event.preventDefault();
    try {
      const auth = await api<{ access_token: string }>(
        "/auth/login",
        undefined,
        { method: "POST", body: JSON.stringify({ email, password }) },
      );
      localStorage.setItem("dataset-platform-token", auth.access_token);
      setToken(auth.access_token);
      notify("已登录到数据集工作台。");
    } catch (error) {
      fail(error);
    }
  };
  const createDataset = async (event: FormEvent) => {
    event.preventDefault();
    if (!org || !newName.trim()) return;
    try {
      await api("/datasets", token, {
        method: "POST",
        body: JSON.stringify({ organization_id: org.id, name: newName.trim() }),
      });
      setNewName("");
      setModal(null);
      await reloadShell();
      notify("数据集已创建。");
    } catch (error) {
      fail(error);
    }
  };
  const deleteDataset = async (event: FormEvent) => {
    event.preventDefault();
    if (!org || !datasetToDelete) return;
    const target = datasetToDelete;
    try {
      await api(`/datasets/${target.id}?organization_id=${org.id}`, token, {
        method: "DELETE",
      });
      const wasActive = dataset?.id === target.id;
      setModal(null);
      setDatasetToDelete(null);
      if (wasActive) {
        setBatchId("");
        setSelectedIds([]);
        setPreview(null);
        setNormalized(null);
      }
      await reloadShell();
      notify(`数据集“${target.name}”已删除。`);
    } catch (error) {
      fail(error);
    }
  };
  const openStorageDialog = async (target: Dataset) => {
    if (!org) return;
    try {
      const preview = await api<PurgePreview>(
        `/datasets/${target.id}/purge-preview?organization_id=${org.id}`,
        token,
      );
      setDatasetToDelete(target);
      setPurgePreview(preview);
      setPurgeConfirmation("");
      setModal("storage");
    } catch (error) {
      fail(error);
    }
  };
  const restoreDataset = async (target: RemovedDataset) => {
    if (!org) return;
    try {
      const restored = await api<Dataset>(
        `/datasets/${target.id}/restore?organization_id=${org.id}`,
        token,
        { method: "POST" },
      );
      setView("workspace");
      setDataset(restored);
      await reloadShell();
      notify(`数据集“${target.name}”已恢复。`);
    } catch (error) {
      fail(error);
    }
  };
  const purgeDataset = async (event: FormEvent) => {
    event.preventDefault();
    if (!org || !datasetToDelete || purgeConfirmation !== datasetToDelete.name)
      return;
    const target = datasetToDelete;
    try {
      await api(
        `/datasets/${target.id}/purge?organization_id=${org.id}`,
        token,
        {
          method: "POST",
          body: JSON.stringify({ confirmation_name: purgeConfirmation }),
        },
      );
      setModal(null);
      setDatasetToDelete(null);
      setPurgePreview(null);
      setPurgeConfirmation("");
      notify(`已开始彻底删除“${target.name}”，可在后台任务中查看进度。`);
      await reloadShell();
    } catch (error) {
      fail(error);
    }
  };
  const directUpload = (url: string, source: File) =>
    new Promise<void>((resolve, reject) => {
      const request = new XMLHttpRequest();
      let reportedProgress = -1;
      request.open("PUT", url);
      request.upload.onprogress = (event) => {
        if (!event.lengthComputable) return;
        const nextProgress = Math.min(
          100,
          Math.floor((event.loaded / event.total) * 100),
        );
        // Progress events can arrive dozens of times per second for large files.
        // Only rerender when the displayed percentage actually changes.
        if (nextProgress !== reportedProgress) {
          reportedProgress = nextProgress;
          setUploadProgress(nextProgress);
        }
      };
      request.onerror = () => reject(new Error("upload.direct_put_failed"));
      request.onabort = () => reject(new Error("upload.cancelled"));
      request.ontimeout = () => reject(new Error("upload.timed_out"));
      request.onload = () =>
        request.status >= 200 && request.status < 300
          ? resolve()
          : reject(new Error("upload.direct_put_failed"));
      request.send(source);
    });
  const upload = async (event: FormEvent) => {
    event.preventDefault();
    if (!dataset || !file) return;
    try {
      setUploadProgress(0);
      const session = await api<Upload>(
        `/datasets/${dataset.id}/upload-sessions`,
        token,
        {
          method: "POST",
          body: JSON.stringify({
            original_name: file.name,
            batch_name: batchName.trim() || "Upload batch",
          }),
        },
      );
      if (!session.upload_url) throw new Error("upload.url_missing");
      await directUpload(session.upload_url, file);
      await api(`/upload-sessions/${session.id}/complete`, token, {
        method: "POST",
        body: "{}",
      });
      setModal(null);
      setFile(null);
      notify("归档已上传，正在安全扫描。扫描完成后请确认导入。");
      await reloadShell();
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    } finally {
      setUploadProgress(null);
    }
  };
  const confirmUpload = async (item: Upload) => {
    try {
      await api(`/upload-sessions/${item.id}/confirm`, token, {
        method: "POST",
        body: "{}",
      });
      notify("已确认导入，正在创建样本索引。");
      await reloadShell();
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const openPreview = async (sample: Sample) => {
    if (!org) return;
    try {
      const next = await api<Preview>(
        `/samples/${sample.id}?organization_id=${org.id}`,
        token,
      );
      setPreview(next);
      setNormalized(
        next.normalized_annotation_url
          ? await fetch(next.normalized_annotation_url).then((response) =>
              response.ok ? response.json() : null,
            )
          : null,
      );
    } catch (error) {
      fail(error);
    }
  };
  const subset = async (ids: string[], value: string | null) => {
    if (!dataset || !org || !ids.length) return;
    try {
      await api(
        `/datasets/${dataset.id}/samples/subset?organization_id=${org.id}`,
        token,
        {
          method: "POST",
          body: JSON.stringify({ sample_ids: ids, subset: value }),
        },
      );
      setSelectedIds([]);
      notify(`已更新 ${ids.length} 个样本的子集。`);
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const deleteSamples = async (ids: string[]) => {
    if (
      !dataset ||
      !org ||
      !ids.length ||
      !window.confirm(
        `确定删除 ${ids.length} 个样本吗？该操作可在历史记录中撤销。`,
      )
    )
      return;
    try {
      await api(
        `/datasets/${dataset.id}/samples/delete?organization_id=${org.id}`,
        token,
        { method: "POST", body: JSON.stringify({ sample_ids: ids }) },
      );
      setSelectedIds([]);
      setPreview(null);
      setNormalized(null);
      notify("样本已删除，可在操作历史中撤销。");
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const quality = async () => {
    if (!dataset || !org) return;
    try {
      await api(
        `/datasets/${dataset.id}/quality-checks?organization_id=${org.id}`,
        token,
        { method: "POST", body: "{}" },
      );
      notify("质量检查任务已加入队列。");
      await reloadShell();
    } catch (error) {
      fail(error);
    }
  };
  const exportData = async (event: FormEvent) => {
    event.preventDefault();
    if (!dataset || !org) return;
    try {
      await api(
        `/datasets/${dataset.id}/exports?organization_id=${org.id}`,
        token,
        {
          method: "POST",
          body: JSON.stringify({
            format: exportFormat,
            import_batch_ids: batchId ? [batchId] : [],
            subsets: exportSubsets,
            class_ids: filters.classId ? [Number(filters.classId)] : [],
            include_unannotated: includeUnannotated,
          }),
        },
      );
      setModal(null);
      notify("导出任务已创建，完成后可在任务队列下载。");
      await reloadShell();
    } catch (error) {
      fail(error);
    }
  };
  const saveBatch = async (event: FormEvent) => {
    event.preventDefault();
    if (!dataset || !org || !editableBatch) return;
    try {
      await api(
        `/datasets/${dataset.id}/import-batches/${editableBatch.id}?organization_id=${org.id}`,
        token,
        {
          method: "PATCH",
          body: JSON.stringify({
            batch_name: editableBatch.batch_name,
            note: editableBatch.note ?? null,
          }),
        },
      );
      setModal(null);
      notify("批次信息已更新。");
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const rescan = async (item: Batch) => {
    if (!dataset || !org) return;
    try {
      await api(
        `/datasets/${dataset.id}/import-batches/${item.id}/rescan?organization_id=${org.id}`,
        token,
        { method: "POST", body: "{}" },
      );
      notify("已开始重新扫描该批次。");
      await reloadShell();
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const deleteBatch = async (item: Batch) => {
    if (
      !dataset ||
      !org ||
      !window.confirm(
        `删除批次“${item.batch_name}”及其样本？可通过历史记录撤销。`,
      )
    )
      return;
    try {
      await api(
        `/datasets/${dataset.id}/import-batches/${item.id}?organization_id=${org.id}`,
        token,
        { method: "DELETE" },
      );
      if (batchId === item.id) setBatchId("");
      notify("批次已删除。");
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const saveLabels = async () => {
    if (!dataset || !org) return;
    try {
      await Promise.all(
        editableLabels
          .filter((item) => item.class_name.trim())
          .map((item) =>
            api(
              `/datasets/${dataset.id}/labels/${item.class_id}?organization_id=${org.id}`,
              token,
              {
                method: "PUT",
                body: JSON.stringify({
                  class_name: item.class_name.trim(),
                  color: item.color || colors[item.class_id % colors.length],
                }),
              },
            ),
          ),
      );
      setModal(null);
      notify("标签映射已保存。");
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const removeLabel = async (item: Label) => {
    if (
      !dataset ||
      !org ||
      !window.confirm(`移除标签“${item.class_name}”？原始标注不会被删除。`)
    )
      return;
    try {
      await api(
        `/datasets/${dataset.id}/labels/${item.class_id}?organization_id=${org.id}`,
        token,
        { method: "DELETE" },
      );
      setEditableLabels((current) =>
        current.filter((label) => label.class_id !== item.class_id),
      );
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  const replay = async (item: History, action: "undo" | "redo") => {
    if (!org) return;
    try {
      await api(
        `/operation-history/${item.id}/${action}?organization_id=${org.id}`,
        token,
        { method: "POST", body: "{}" },
      );
      notify(action === "undo" ? "操作已撤销。" : "操作已恢复。");
      await reloadWorkspace();
    } catch (error) {
      fail(error);
    }
  };
  if (!token)
    return (
      <main className="auth-shell">
        <section className="auth-card">
          <div className="auth-mark">DP</div>
          <p className="kicker">LOCAL-FIRST / TEAM READY</p>
          <h1>数据集工作台</h1>
          <p>导入、检查、浏览和导出视觉数据集的一体化控制台。</p>
          <form onSubmit={signIn}>
            <label>
              邮箱
              <input
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="username"
              />
            </label>
            <label>
              密码
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                placeholder="输入部署密码"
              />
            </label>
            <button className="primary full">
              进入工作台 <span>→</span>
            </button>
          </form>
          {notice && <p className="form-notice">{notice}</p>}
        </section>
      </main>
    );

  return (
    <main className="app-shell">
      <aside className="dataset-nav">
        <div className="brand">
          <span className="brand-mark">DP</span>
          <div>
            <strong>Dataset</strong>
            <small>WORKBENCH</small>
          </div>
        </div>
        <div className="workspace-name">
          <span className="live-dot" />
          {org?.name ?? "正在载入工作区"}
        </div>
        <button
          className="primary new-dataset"
          onClick={() => setModal("create")}
        >
          + 新建数据集
        </button>
        <div className="nav-heading">
          <span>数据集</span>
          <small>{datasets.length}</small>
        </div>
        <nav className="dataset-list">
          {datasets.length ? (
            datasets.map((item) => (
              <div
                key={item.id}
                className={
                  dataset?.id === item.id
                    ? "dataset-item active"
                    : "dataset-item"
                }
              >
                <button
                  className="dataset-select"
                  onClick={() => {
                    setDataset(item);
                    setView("workspace");
                  }}
                >
                  <span className="dataset-initial">
                    {item.name.slice(0, 1).toUpperCase()}
                  </span>
                  <span>
                    <strong>{item.name}</strong>
                    <small>
                      {new Date(
                        item.updated_at || item.created_at,
                      ).toLocaleDateString()}
                    </small>
                  </span>
                </button>
                <button
                  className="dataset-delete"
                  type="button"
                  title={`删除数据集 ${item.name}`}
                  aria-label={`删除数据集 ${item.name}`}
                  onClick={() => {
                    setDatasetToDelete(item);
                    setModal("delete-dataset");
                  }}
                >
                  ×
                </button>
              </div>
            ))
          ) : (
            <p className="empty-nav">创建首个数据集以开始工作。</p>
          )}
        </nav>
        <div className="nav-footer">
          <button
            className={
              view === "trash" ? "recycle-link active" : "recycle-link"
            }
            onClick={() => setView("trash")}
          >
            <span>{"\u2672"}</span> 回收站 <b>{removedDatasets.length}</b>
          </button>
          <button
            onClick={() => {
              localStorage.removeItem("dataset-platform-token");
              setToken("");
            }}
          >
            退出登录
          </button>
        </div>
      </aside>
      <section className="workbench">
        {view === "trash" ? (
          <section className="trash-page">
            <div className="trash-page-header">
              <div>
                <p className="kicker">RECYCLE BIN</p>
                <h1>已移除数据集</h1>
                <p>
                  移除的数据集仍保留在存储中；可恢复，或彻底删除以释放空间。
                </p>
              </div>
              <span>{removedDatasets.length} 个数据集</span>
            </div>
            <div className="trash-grid">
              {removedDatasets.length ? (
                removedDatasets.map((item) => (
                  <article className="trash-card" key={item.id}>
                    <div className="trash-card-heading">
                      <span className="dataset-initial">
                        {item.name.slice(0, 1).toUpperCase()}
                      </span>
                      <div>
                        <strong title={item.name}>{item.name}</strong>
                        <small>
                          移除于 {new Date(item.deleted_at).toLocaleString()}
                        </small>
                      </div>
                    </div>
                    <div className="trash-metrics">
                      <span>
                        占用空间 <b>{formatBytes(item.estimated_bytes)}</b>
                      </span>
                      <span>
                        {item.sample_count.toLocaleString()} 个样本 {"\u00b7"}{" "}
                        {item.batch_count} 个批次
                      </span>
                    </div>
                    <div className="trash-actions">
                      <button onClick={() => void restoreDataset(item)}>
                        恢复数据集
                      </button>
                      <button onClick={() => void openStorageDialog(item)}>
                        查看占用空间
                      </button>
                    </div>
                  </article>
                ))
              ) : (
                <div className="trash-empty">
                  <span>{"\u2672"}</span>
                  <strong>回收站为空</strong>
                  <p>从工作区移除的数据集会显示在这里。</p>
                </div>
              )}
            </div>
          </section>
        ) : !dataset ? (
          <section className="welcome">
            <div className="welcome-grid" />
            <p className="kicker">START HERE</p>
            <h1>建立第一个数据集</h1>
            <p>新建数据集后，可直接导入 ZIP、7z 或 TAR 系列归档。</p>
            <button className="primary" onClick={() => setModal("create")}>
              新建数据集
            </button>
          </section>
        ) : (
          <>
            <header className="workbench-header">
              <div>
                <p className="breadcrumb">
                  工作区 / 数据集 / <b>{dataset.name}</b>
                </p>
                <h1>{dataset.name}</h1>
                <p className="dataset-description">
                  {dataset.description || "视觉数据集管理与质量控制"}
                </p>
              </div>
              <div className="header-actions">
                <button onClick={() => inputRef.current?.click()}>
                  ↑ 导入批次
                </button>
                <button onClick={() => setModal("export")}>⇩ 导出</button>
                <button
                  onClick={() => {
                    setEditableLabels(labels);
                    setModal("labels");
                  }}
                >
                  标签
                </button>
                <button onClick={quality}>质量检查</button>
                <button
                  className={showStats ? "selected-action" : ""}
                  onClick={() => setShowStats((value) => !value)}
                >
                  统计
                </button>
                <button onClick={() => setModal("history")}>历史</button>
              </div>
            </header>
            <input
              ref={inputRef}
              className="sr-only"
              type="file"
              accept=".zip,.7z,.tar,.tar.gz,.tgz,.tar.bz2,.tbz2,.tar.xz,.txz"
              onChange={(event) => {
                const source = event.target.files?.[0];
                if (source) {
                  setFile(source);
                  setBatchName(
                    source.name.replace(
                      /(\.tar\.gz|\.tar\.bz2|\.tar\.xz|\.zip|\.7z|\.tgz|\.tbz2|\.txz|\.tar)$/i,
                      "",
                    ) || "Upload batch",
                  );
                  setModal("upload");
                }
                event.currentTarget.value = "";
              }}
            />
            <section className="metric-strip">
              <div>
                <small>样本总数</small>
                <b>{stats?.sample_count ?? sampleTotal}</b>
                <span>INDEXED</span>
              </div>
              <div>
                <small>已标注</small>
                <b>{stats?.annotated_sample_count ?? 0}</b>
                <span>ANNOTATED</span>
              </div>
              <div>
                <small>训练集</small>
                <b>{stats?.by_subset?.train ?? 0}</b>
                <span>TRAIN</span>
              </div>
              <div>
                <small>缺失标注</small>
                <b
                  className={
                    (stats?.missing_annotation_count ?? 0) > 0
                      ? "warning-number"
                      : ""
                  }
                >
                  {stats?.missing_annotation_count ?? 0}
                </b>
                <span>REVIEW</span>
              </div>
              <div className="metric-format">
                <small>标注类型</small>
                <div>
                  {Object.entries(stats?.by_annotation_type ?? {})
                    .slice(0, 3)
                    .map(([name, count]) => (
                      <span key={name}>
                        {name} <b>{count}</b>
                      </span>
                    ))}
                </div>
              </div>
            </section>
            {showStats && (
              <section className="stats-panel">
                <div className="panel-title">
                  <div>
                    <p className="kicker">DATA PROFILE</p>
                    <h2>数据分布</h2>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setShowStats(false)}
                  >
                    收起
                  </button>
                </div>
                <div className="stat-bars">
                  {Object.entries(stats?.by_subset ?? {}).map(
                    ([name, count]) => (
                      <div key={name}>
                        <span>{name}</span>
                        <i>
                          <b
                            style={{
                              width: `${Math.max(3, Math.round((count / Math.max(stats?.sample_count || 1, 1)) * 100))}%`,
                            }}
                          />
                        </i>
                        <strong>{count}</strong>
                      </div>
                    ),
                  )}
                </div>
                <div className="class-cloud">
                  {(stats?.class_distribution ?? [])
                    .slice(0, 10)
                    .map((item) => (
                      <span key={item.class_id}>
                        <i
                          style={{
                            background: colors[item.class_id % colors.length],
                          }}
                        />
                        {item.class_name}
                        <b>{item.sample_count}</b>
                      </span>
                    ))}
                </div>
              </section>
            )}
            <section className="workspace-grid">
              <aside className="parts-panel panel">
                <div className="panel-title">
                  <div>
                    <p className="kicker">SOURCE STRUCTURE</p>
                    <h2>导入批次</h2>
                  </div>
                  <button
                    className="text-button"
                    onClick={() => setBatchId("")}
                  >
                    全部
                  </button>
                </div>
                <button
                  className={batchId === "" ? "part-all active" : "part-all"}
                  onClick={() => setBatchId("")}
                >
                  <span>◫</span>
                  <b>全部样本</b>
                  <em>{stats?.sample_count ?? sampleTotal}</em>
                </button>
                <div className="parts-list">
                  {batches.map((item) => {
                    const pendingUpload = uploads.find(
                      (upload) =>
                        upload.import_batch_id === item.id &&
                        upload.status === "waiting_confirmation",
                    );
                    return (
                      <article
                        key={item.id}
                        className={
                          batchId === item.id ? "part-card active" : "part-card"
                        }
                      >
                        <button
                          className="part-select"
                          onClick={() => setBatchId(item.id)}
                        >
                          <span className="part-number">
                            {String(item.batch_number).padStart(2, "0")}
                          </span>
                          <span className="part-copy">
                            <strong title={item.batch_name}>
                              {item.batch_name}
                            </strong>
                            <small>
                              {item.source_type} {"\u00b7"}{" "}
                              {new Date(item.created_at).toLocaleDateString()}
                            </small>
                          </span>
                          <span className={`batch-status ${item.status}`}>
                            {batchStatusLabel(item.status)}
                          </span>
                        </button>
                        <div className="part-card-actions">
                          {pendingUpload && (
                            <button
                              className="confirm-batch"
                              onClick={() => confirmUpload(pendingUpload)}
                            >
                              {"\u786e\u8ba4\u5bfc\u5165"}
                            </button>
                          )}
                          <button
                            onClick={() => {
                              setEditableBatch({ ...item });
                              setModal("batch");
                            }}
                          >
                            {"\u7f16\u8f91"}
                          </button>
                          <button onClick={() => rescan(item)}>
                            {"\u91cd\u626b"}
                          </button>
                          <button
                            className="danger-text"
                            onClick={() => deleteBatch(item)}
                          >
                            {"\u5220\u9664"}
                          </button>
                        </div>
                      </article>
                    );
                  })}
                </div>
                <div className="parts-hint">
                  选择批次可将样本表和导出范围限制在该 Part。
                </div>
              </aside>
              <section className="samples-panel panel">
                <div className="panel-title samples-title">
                  <div>
                    <p className="kicker">SAMPLE BROWSER</p>
                    <h2>
                      {selectedBatch ? selectedBatch.batch_name : "全部样本"}
                    </h2>
                  </div>
                  <span>{sampleTotal} 条记录</span>
                </div>
                <div className="filter-grid">
                  <label className="search-field">
                    <span>⌕</span>
                    <input
                      value={filters.name}
                      onChange={(event) =>
                        setFilters({ ...filters, name: event.target.value })
                      }
                      placeholder="搜索文件名…"
                    />
                  </label>
                  <select
                    value={filters.subset}
                    onChange={(event) =>
                      setFilters({ ...filters, subset: event.target.value })
                    }
                  >
                    <option value="">全部子集</option>
                    <option value="train">train</option>
                    <option value="val">val</option>
                    <option value="test">test</option>
                  </select>
                  <select
                    value={filters.format}
                    onChange={(event) =>
                      setFilters({ ...filters, format: event.target.value })
                    }
                  >
                    <option value="">全部格式</option>
                    <option value="yolo">YOLO</option>
                    <option value="coco">COCO</option>
                    <option value="labelme">LabelMe</option>
                    <option value="voc">VOC</option>
                  </select>
                  <select
                    value={filters.classId}
                    onChange={(event) =>
                      setFilters({ ...filters, classId: event.target.value })
                    }
                  >
                    <option value="">全部类别</option>
                    {labels.map((item) => (
                      <option key={item.class_id} value={item.class_id}>
                        {item.class_id} · {item.class_name}
                      </option>
                    ))}
                  </select>
                  <select
                    value={filters.annotation}
                    onChange={(event) =>
                      setFilters({ ...filters, annotation: event.target.value })
                    }
                  >
                    <option value="">标注状态</option>
                    <option value="true">已有标注</option>
                    <option value="false">缺失标注</option>
                  </select>
                </div>
                {selectedIds.length > 0 && (
                  <div className="bulk-bar">
                    <b>已选 {selectedIds.length}</b>
                    <span>移动到</span>
                    {["train", "val", "test"].map((name) => (
                      <button
                        key={name}
                        onClick={() => subset(selectedIds, name)}
                      >
                        {name}
                      </button>
                    ))}
                    <button
                      className="danger-text"
                      onClick={() => deleteSamples(selectedIds)}
                    >
                      删除
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setSelectedIds([])}
                    >
                      取消
                    </button>
                  </div>
                )}
                <div
                  className="sample-table"
                  ref={samplesScrollRef}
                  onScroll={(event) => {
                    const element = event.currentTarget;
                    if (
                      element.scrollHeight -
                        element.scrollTop -
                        element.clientHeight <
                      180
                    )
                      void loadMoreSamples().catch(fail);
                  }}
                >
                  <div className="sample-head">
                    <label>
                      <input
                        type="checkbox"
                        checked={
                          samples.length > 0 &&
                          samples.every((item) => selectedIds.includes(item.id))
                        }
                        onChange={(event) =>
                          setSelectedIds(
                            event.target.checked
                              ? samples.map((item) => item.id)
                              : [],
                          )
                        }
                      />
                    </label>
                    <span>文件</span>
                    <span>子集</span>
                    <span>标注</span>
                    <span>尺寸</span>
                  </div>
                  {samples.length ? (
                    samples.map((item) => (
                      <div
                        className={
                          preview?.sample.id === item.id
                            ? "sample-row chosen"
                            : "sample-row"
                        }
                        key={item.id}
                      >
                        <label onClick={(event) => event.stopPropagation()}>
                          <input
                            type="checkbox"
                            checked={selectedIds.includes(item.id)}
                            onChange={(event) =>
                              setSelectedIds((current) =>
                                event.target.checked
                                  ? [...new Set([...current, item.id])]
                                  : current.filter((id) => id !== item.id),
                              )
                            }
                          />
                        </label>
                        <button onClick={() => openPreview(item)}>
                          <strong title={item.relative_path}>
                            {item.file_name}
                          </strong>
                          <small>{item.relative_path}</small>
                        </button>
                        <button
                          onClick={() =>
                            subset(
                              [item.id],
                              item.subset === "train" ? "val" : "train",
                            )
                          }
                        >
                          <span
                            className={`subset-chip ${item.subset || "none"}`}
                          >
                            {item.subset || "未分配"}
                          </span>
                        </button>
                        <span className="annotation-type">
                          {item.annotation_type || "—"}
                        </span>
                        <span>
                          {item.width && item.height
                            ? `${item.width}×${item.height}`
                            : "—"}
                        </span>
                      </div>
                    ))
                  ) : (
                    <div className="table-empty">
                      <span>⌁</span>
                      <strong>没有匹配的样本</strong>
                      <p>调整筛选条件，或导入新的数据归档。</p>
                    </div>
                  )}
                </div>
                <footer className="table-footer">
                  <span>
                    {sampleTotal
                      ? `已加载 ${samples.length} / ${sampleTotal} 条记录`
                      : `0 条记录`}
                  </span>
                  {samples.length < sampleTotal && (
                    <span className="scroll-hint">向下滑动继续加载</span>
                  )}
                </footer>
              </section>
              <aside className="preview-panel panel">
                <div className="panel-title">
                  <div>
                    <p className="kicker">INSPECTOR</p>
                    <h2>样本预览</h2>
                  </div>
                  {preview && (
                    <button
                      className="text-button"
                      onClick={() => {
                        setPreview(null);
                        setNormalized(null);
                      }}
                    >
                      关闭
                    </button>
                  )}
                </div>
                {preview ? (
                  <div className="preview-content">
                    <div className="preview-image">
                      {preview.image_url ? (
                        <>
                          <img
                            src={preview.image_url}
                            alt={preview.sample.file_name}
                          />
                          {normalized && <Overlay payload={normalized} />}
                        </>
                      ) : (
                        <div className="image-fallback">图片资源不可用</div>
                      )}
                    </div>
                    <div className="preview-file">
                      <strong>{preview.sample.file_name}</strong>
                      <small>{preview.sample.relative_path}</small>
                    </div>
                    <div className="preview-counts">
                      <span>
                        <b>{preview.summary?.annotation_count ?? 0}</b> 标注
                      </span>
                      <span>
                        <b>{preview.summary?.bbox_count ?? 0}</b> 框
                      </span>
                      <span>
                        <b>{preview.summary?.polygon_count ?? 0}</b> 多边形
                      </span>
                    </div>
                    <div className="label-legend">
                      {normalized?.annotations
                        .slice(0, 6)
                        .map((item, index) => (
                          <span key={`${item.class_id}-${index}`}>
                            <i
                              style={{
                                background:
                                  colors[item.class_id % colors.length],
                              }}
                            />
                            {item.class_name || `class_${item.class_id}`}
                          </span>
                        ))}
                    </div>
                    <div className="preview-actions">
                      {preview.annotation_url && (
                        <a
                          href={preview.annotation_url}
                          target="_blank"
                          rel="noreferrer"
                        >
                          打开原始标注 ↗
                        </a>
                      )}
                      <div>
                        <button
                          onClick={() => subset([preview.sample.id], "train")}
                        >
                          设为 train
                        </button>
                        <button
                          onClick={() => deleteSamples([preview.sample.id])}
                          className="danger-text"
                        >
                          删除样本
                        </button>
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="preview-empty">
                    <div className="preview-placeholder">
                      <span>⌖</span>
                    </div>
                    <strong>选择一个样本</strong>
                    <p>在表格中点击样本，检查图片、标注和元数据。</p>
                  </div>
                )}
                <section className="quality-mini">
                  <div className="section-mini-title">
                    <span>质量问题</span>
                    <button className="text-button" onClick={quality}>
                      运行检查
                    </button>
                  </div>
                  {issues.length ? (
                    issues.map((item) => (
                      <div className="issue-line" key={item.id}>
                        <i className={item.severity} />
                        <span>
                          <strong>{item.issue_type}</strong>
                          <small>{item.detail_code}</small>
                        </span>
                      </div>
                    ))
                  ) : (
                    <p>暂无已发现问题。</p>
                  )}
                </section>
              </aside>
            </section>
          </>
        )}
        {notice && (
          <div className="toast">
            <span>✓</span>
            {notice}
          </div>
        )}
      </section>
      {modal === "delete-dataset" && datasetToDelete && (
        <Modal
          title="移除数据集"
          onClose={() => {
            setModal(null);
            setDatasetToDelete(null);
          }}
        >
          <form className="modal-form" onSubmit={deleteDataset}>
            <p className="danger-copy">
              数据集会从工作区移至回收站，样本和文件会继续保留，之后可随时恢复。
            </p>
            <p className="scope-note">
              如需彻底删除并释放空间，请在回收站内查看占用后操作。
            </p>
            <div className="modal-actions">
              <button
                type="button"
                onClick={() => {
                  setModal(null);
                  setDatasetToDelete(null);
                }}
              >
                取消
              </button>
              <button className="danger-button">移至回收站</button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "storage" && datasetToDelete && purgePreview && (
        <Modal
          title="数据集占用空间"
          onClose={() => {
            setModal(null);
            setDatasetToDelete(null);
          }}
        >
          <div className="modal-form">
            <p>当前数据集的存储清单如下。彻底删除后将不可恢复。</p>
            <div className="purge-summary">
              <span>
                样本 <b>{purgePreview.sample_count.toLocaleString()}</b>
              </span>
              <span>
                导入批次 <b>{purgePreview.batch_count}</b>
              </span>
              <span>
                对象文件 <b>{purgePreview.object_count.toLocaleString()}</b>
              </span>
              <span>
                预计释放 <b>{formatBytes(purgePreview.estimated_bytes)}</b>
              </span>
            </div>
            <div className="modal-actions">
              <button
                onClick={() => {
                  setModal(null);
                  setDatasetToDelete(null);
                }}
              >
                关闭
              </button>
              <button
                className="danger-button"
                onClick={() => {
                  setPurgeConfirmation("");
                  setModal("purge-dataset");
                }}
              >
                彻底删除并释放空间
              </button>
            </div>
          </div>
        </Modal>
      )}
      {modal === "purge-dataset" && datasetToDelete && purgePreview && (
        <Modal
          title="彻底删除数据集"
          onClose={() => {
            setModal("storage");
            setPurgeConfirmation("");
          }}
        >
          <form className="modal-form" onSubmit={purgeDataset}>
            <p className="danger-copy">
              此操作不可恢复。将删除“{purgePreview.dataset_name}
              ”的原始归档、图像、标注、索引与数据库记录。
            </p>
            <div className="purge-summary">
              <span>
                样本 <b>{purgePreview.sample_count.toLocaleString()}</b>
              </span>
              <span>
                导入批次 <b>{purgePreview.batch_count}</b>
              </span>
              <span>
                对象文件 <b>{purgePreview.object_count.toLocaleString()}</b>
              </span>
              <span>
                预计释放 <b>{formatBytes(purgePreview.estimated_bytes)}</b>
              </span>
              {purgePreview.stale_temp_directory_count > 0 && (
                <span>
                  可清理临时目录{" "}
                  <b>
                    {purgePreview.stale_temp_directory_count} /{" "}
                    {formatBytes(purgePreview.stale_temp_bytes)}
                  </b>
                </span>
              )}
            </div>
            <label>
              请输入数据集名称 <b>{purgePreview.dataset_name}</b> 以确认
              <input
                autoFocus
                value={purgeConfirmation}
                onChange={(event) => setPurgeConfirmation(event.target.value)}
              />
            </label>
            <div className="modal-actions">
              <button
                type="button"
                onClick={() => {
                  setModal("storage");
                  setPurgeConfirmation("");
                }}
              >
                返回
              </button>
              <button
                className="danger-button"
                disabled={purgeConfirmation !== purgePreview.dataset_name}
              >
                确认彻底删除
              </button>
            </div>
          </form>
        </Modal>
      )}

      {modal === "create" && (
        <Modal title="新建数据集" onClose={() => setModal(null)}>
          <form className="modal-form" onSubmit={createDataset}>
            <p>数据集将保留导入批次、样本索引、标签映射和可撤销的操作历史。</p>
            <label>
              数据集名称
              <input
                autoFocus
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                placeholder="例如：road-defect-2026"
              />
            </label>
            <div className="modal-actions">
              <button type="button" onClick={() => setModal(null)}>
                取消
              </button>
              <button className="primary" disabled={!newName.trim()}>
                创建数据集
              </button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "upload" && (
        <Modal title="导入归档为新批次" onClose={() => setModal(null)}>
          <form className="modal-form" onSubmit={upload}>
            <div className="file-summary">
              <span>↥</span>
              <div>
                <strong>{file?.name}</strong>
                <small>
                  {file ? `${(file.size / 1024 / 1024).toFixed(1)} MB` : ""}
                </small>
              </div>
            </div>
            <label>
              批次名称
              <input
                autoFocus
                value={batchName}
                onChange={(event) => setBatchName(event.target.value)}
              />
            </label>
            <p className="muted">
              系统会先安全扫描 ZIP、7z 或 TAR
              归档；扫描预览确认后才会正式写入样本索引。
            </p>
            {uploadProgress !== null && (
              <div className="upload-progress">
                <i style={{ width: `${uploadProgress}%` }} />
                <span>{uploadProgress}%</span>
              </div>
            )}
            <div className="modal-actions">
              <button type="button" onClick={() => setModal(null)}>
                取消
              </button>
              <button className="primary">上传并扫描</button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "export" && (
        <Modal title="导出数据集" onClose={() => setModal(null)}>
          <form className="modal-form" onSubmit={exportData}>
            <p>导出会作为后台任务生成，可在任务队列中下载结果。</p>
            <label>
              导出格式
              <select
                value={exportFormat}
                onChange={(event) => setExportFormat(event.target.value)}
              >
                <option value="coco">COCO JSON</option>
                <option value="yolo">YOLO</option>
                <option value="labelme">LabelMe</option>
                <option value="voc">Pascal VOC</option>
                <option value="manifest">JSON Manifest</option>
              </select>
            </label>
            <fieldset>
              <legend>子集范围</legend>
              {["train", "val", "test"].map((name) => (
                <label className="check-line" key={name}>
                  <input
                    type="checkbox"
                    checked={exportSubsets.includes(name)}
                    onChange={(event) =>
                      setExportSubsets((current) =>
                        event.target.checked
                          ? [...current, name]
                          : current.filter((item) => item !== name),
                      )
                    }
                  />
                  {name}
                </label>
              ))}
            </fieldset>
            <label className="check-line">
              <input
                type="checkbox"
                checked={includeUnannotated}
                onChange={(event) =>
                  setIncludeUnannotated(event.target.checked)
                }
              />
              保留未标注图像
            </label>
            {batchId && (
              <p className="scope-note">
                当前仅导出批次：{selectedBatch?.batch_name}
              </p>
            )}
            <div className="modal-actions">
              <button type="button" onClick={() => setModal(null)}>
                取消
              </button>
              <button className="primary">创建导出任务</button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "batch" && editableBatch && (
        <Modal title="编辑导入批次" onClose={() => setModal(null)}>
          <form className="modal-form" onSubmit={saveBatch}>
            <label>
              批次名称
              <input
                autoFocus
                value={editableBatch.batch_name}
                onChange={(event) =>
                  setEditableBatch({
                    ...editableBatch,
                    batch_name: event.target.value,
                  })
                }
              />
            </label>
            <label>
              备注
              <textarea
                value={editableBatch.note || ""}
                onChange={(event) =>
                  setEditableBatch({
                    ...editableBatch,
                    note: event.target.value,
                  })
                }
                placeholder="记录采集来源、标注说明或版本信息"
              />
            </label>
            <div className="modal-actions">
              <button type="button" onClick={() => setModal(null)}>
                取消
              </button>
              <button className="primary">保存更改</button>
            </div>
          </form>
        </Modal>
      )}
      {modal === "labels" && (
        <Modal title="标签映射管理" wide onClose={() => setModal(null)}>
          <div className="label-manager">
            <p>名称和颜色会用于浏览、统计和导出；修改不会覆盖原始标注文件。</p>
            <div className="label-table">
              <div className="label-row label-head">
                <span>ID</span>
                <span>类别名称</span>
                <span>颜色</span>
                <span />
              </div>
              {editableLabels.map((item, index) => (
                <div className="label-row" key={`${item.class_id}-${index}`}>
                  <span>{item.class_id}</span>
                  <input
                    value={item.class_name}
                    onChange={(event) =>
                      setEditableLabels((current) =>
                        current.map((label, labelIndex) =>
                          labelIndex === index
                            ? { ...label, class_name: event.target.value }
                            : label,
                        ),
                      )
                    }
                  />
                  <input
                    type="color"
                    value={item.color || colors[item.class_id % colors.length]}
                    onChange={(event) =>
                      setEditableLabels((current) =>
                        current.map((label, labelIndex) =>
                          labelIndex === index
                            ? { ...label, color: event.target.value }
                            : label,
                        ),
                      )
                    }
                  />
                  <button
                    className="danger-text"
                    onClick={() => removeLabel(item)}
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
            <button
              className="add-label"
              onClick={() =>
                setEditableLabels((current) => [
                  ...current,
                  {
                    class_id:
                      Math.max(-1, ...current.map((item) => item.class_id)) + 1,
                    class_name: "新类别",
                    color: colors[current.length % colors.length],
                  },
                ])
              }
            >
              + 添加类别
            </button>
            <div className="modal-actions">
              <button onClick={() => setModal(null)}>取消</button>
              <button className="primary" onClick={saveLabels}>
                保存标签映射
              </button>
            </div>
          </div>
        </Modal>
      )}
      {modal === "history" && (
        <Modal title="操作历史" wide onClose={() => setModal(null)}>
          <div className="history-list">
            {history.length ? (
              history.map((item) => (
                <article key={item.id}>
                  <i className={item.status} />
                  <div>
                    <strong>{item.summary}</strong>
                    <small>
                      {item.action} ·{" "}
                      {new Date(item.created_at).toLocaleString()}
                    </small>
                  </div>
                  <span className={`history-status ${item.status}`}>
                    {item.status === "applied" ? "已应用" : "已撤销"}
                  </span>
                  {item.status === "applied" ? (
                    <button onClick={() => replay(item, "undo")}>撤销</button>
                  ) : (
                    <button onClick={() => replay(item, "redo")}>重做</button>
                  )}
                </article>
              ))
            ) : (
              <p className="muted">该数据集还没有可撤销的操作。</p>
            )}
          </div>
        </Modal>
      )}
    </main>
  );
}
