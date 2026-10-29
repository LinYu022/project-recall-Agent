import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const states = {
  DRAFT: "计划就绪",
  WAITING_INPUT: "等待澄清",
  WAITING_APPROVAL: "等待授权",
  RUNNING: "运行中",
  PAUSED: "已暂停",
  COMPLETED: "运行完成",
  FAILED: "运行失败",
  CANCELLED: "已取消",
};
const dispositions = {
  answered: "已形成回答 · 仍需语义核查",
  insufficient_evidence: "证据不足",
  no_matching_records: "允许来源内无匹配",
  needs_clarification: "需补充条件",
  unsupported_time: "不支持该时间范围",
};
const fmt = (v) => JSON.stringify(v, null, 2);
const empty = {
  make: "TOYOTA",
  model: "PRIUS",
  model_year: "2010",
  campaign_number: "",
  production_date: "",
  configurations: "{}",
  question: "核对 ABS 制动问题、风险与补救，说明证据边界。",
  intent: "record_lookup",
  query: "ABS brake risk remedy",
  pairs: "[]",
  time: "snapshot_analysis",
  cutoff: "2010-02-01T00:00:00Z",
};

function Data({ value }) {
  return <pre>{fmt(value)}</pre>;
}
function EvidenceBody({ card }) {
  const p = card.payload;
  return (
    <>
      {p.text && <blockquote>{p.text}</blockquote>}
      {p.counts && (
        <p>
          确认匹配 {p.counts.matched} 条 · 待定 {p.counts.pending} 条 · 排除{" "}
          {p.counts.excluded}{" "}
          条。范围仅限本次允许的冻结来源；零匹配不能证明全球不存在召回。
        </p>
      )}
      {card.kind === "relationship" && (
        <p>
          以下关系由固定规则生成；重叠范围、相同症状和官方引用须分别理解，不能证明同一缺陷或个车适用。
        </p>
      )}
      <details>
        <summary>完整证据与定位信息</summary>
        <Data value={p} />
      </details>
    </>
  );
}
function App() {
  const [token, setToken] = useState(""),
    auth = useRef("");
  const [info, setInfo] = useState(null),
    [catalog, setCatalog] = useState([]),
    [release, setRelease] = useState(""),
    [sources, setSources] = useState([]);
  const [form, setForm] = useState(empty),
    [task, setTask] = useState(null),
    [list, setList] = useState([]),
    [report, setReport] = useState(null),
    [history, setHistory] = useState(null),
    [dispatch, setDispatch] = useState(null),
    [research, setResearch] = useState(null);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false),
    [tab, setTab] = useState("任务"),
    [memory, setMemory] = useState([]),
    [detail, setDetail] = useState(null),
    [planText, setPlanText] = useState(""),
    [answers, setAnswers] = useState({});
  const [pairRecords, setPairRecords] = useState([]);
  const selected = useRef(null),
    generation = useRef(0),
    pending = useRef(false);
  const taskOffset = useRef(0);
  const polling = useRef(false);
  async function api(path, body) {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        Authorization: `Bearer ${auth.current}`,
        "Content-Type": "application/json",
      },
      ...(body === undefined ? {} : { body: fmt(body) }),
    });
    const data = await response.json();
    if (!response.ok) {
      const e = new Error(
        response.status === 409
          ? "页面版本已过期。已刷新状态，请重新检查后操作。"
          : response.status === 403
            ? "没有当前操作的权限，或授权已撤销。"
            : `操作失败（${response.status}）：${data.status || data.detail || "请求无效"}`,
      );
      e.status = response.status;
      throw e;
    }
    return data;
  }
  async function refresh(id = selected.current) {
    const ticket = generation.current;
    const tasks = await api(
      `/product/tasks?offset=${taskOffset.current}&limit=30`,
    );
    if (ticket !== generation.current) return;
    setList(tasks.tasks);
    if (!id) return;
    const t = await api(`/tasks/${id}`);
    if (ticket !== generation.current || id !== selected.current) return;
    setTask(t);
    const d = await api(`/tasks/${id}/dispatch`);
    if (ticket !== generation.current || id !== selected.current) return;
    setDispatch(d);
    if (t.state === "COMPLETED" && t.authorization?.active) {
      const r = await api(`/tasks/${id}/report`);
      if (ticket === generation.current && id === selected.current)
        setReport(r);
    } else {
      setReport(null);
      if (!t.authorization?.active) {
        setDetail(null);
        setResearch(null);
        setHistory(null);
      }
    }
  }
  async function run(fn) {
    if (pending.current) return;
    pending.current = true;
    generation.current++;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      setError(e.message);
      setDetail(null);
      setReport(null);
      // Never keep protected content visible after a read or authorization failure.
      if (e.status === 403) {
        setReport(null);
        setResearch(null);
        setHistory(null);
      }
      if (e.status === 409) {
        setReport(null);
        await refresh().catch(() => {});
      }
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }
  useEffect(() => {
    fetch("/product/info")
      .then((r) => r.json())
      .then(setInfo)
      .catch(() => setError("服务不可用"));
  }, []);
  useEffect(() => {
    if (!catalog.length) return;
    const interval = setInterval(() => {
      if (pending.current || polling.current) return;
      polling.current = true;
      const ticket = generation.current;
      refresh()
        .catch((e) => {
          if (ticket !== generation.current || pending.current) return;
          setError(e.message);
          setReport(null);
          setDetail(null);
          setResearch(null);
          setHistory(null);
        })
        .finally(() => {
          polling.current = false;
        });
    }, 2000);
    return () => clearInterval(interval);
  }, [catalog.length]);
  async function connect() {
    generation.current++;
    selected.current = null;
    setTask(null);
    setReport(null);
    setResearch(null);
    setHistory(null);
    setDetail(null);
    setMemory([]);
    setCatalog([]);
    setList([]);
    taskOffset.current = 0;
    auth.current = token.trim();
    setToken("");
    const c = await api("/product/catalog");
    setCatalog(c.releases);
    if (c.releases[0]) {
      setRelease(c.releases[0].release_id);
      setSources(c.releases[0].source_ids);
    }
    await refresh(null);
    setNotice("已连接。凭据仅保留在当前页面内存中。");
  }
  async function select(id) {
    generation.current++;
    selected.current = id;
    setTask(null);
    setReport(null);
    setHistory(null);
    setResearch(null);
    setDetail(null);
    setDispatch(null);
    setAnswers({});
    setPairRecords([]);
    await refresh(id);
  }
  async function submit() {
    const id = "UI-" + crypto.randomUUID().slice(0, 8).toUpperCase();
    const scope = Object.fromEntries(
      ["make", "model", "campaign_number", "production_date"]
        .filter((k) => form[k].trim())
        .map((k) => [k, form[k].trim()]),
    );
    if (form.model_year) scope.model_year = Number(form.model_year);
    scope.configurations = JSON.parse(form.configurations);
    const request = {
      task_id: id,
      question: form.question,
      retrieval_query: form.query,
      intent: form.intent,
      pairs: JSON.parse(form.pairs),
    };
    if (form.time === "historical_as_of")
      request.expected_time = {
        mode: form.time,
        event_interval: null,
        knowledge_cutoff: form.cutoff,
        date_filter_basis: "用户要求当时可知资料",
      };
    await api("/tasks", {
      task_id: id,
      plan: {
        release_id: release,
        allowed_source_ids: sources,
        scope,
        request,
        mode: "PLAN",
        investigator: "fixed",
        hypotheses: ["检查支持与反证，保留未知条件"],
        steps: ["核对范围", "检索原文与关系", "检查证据边界"],
      },
    });
    await select(id);
    setNotice("计划已创建。生成回答前需单独检查并批准执行版本。");
  }
  async function mutate(action, body = {}) {
    await api(`/tasks/${task.task_id}/${action}`, {
      expected_version: task.version,
      ...body,
    });
    setReport(null);
    setDetail(null);
    setResearch(null);
    setHistory(null);
    await refresh();
  }
  async function inspect(alias) {
    const page = await api(
      `/tasks/${task.task_id}/context?section=evidence&alias=${alias}&limit=4000`,
    );
    const original = await api(
      `/product/tasks/${task.task_id}/original/${alias}`,
    );
    setDetail({ alias, page, original });
  }
  function preset(kind) {
    const ford = kind === "ford";
    setForm({
      ...empty,
      ...(ford
        ? {
            make: "FORD",
            model: "F-150",
            model_year: "2016",
            intent: "relationship",
            query: "door latch cable handle",
            question:
              "比较两项车门召回的条件、缺陷、风险与补救。同车型同部件能否证明同一缺陷或个车适用？",
          }
        : {}),
      ...(kind === "missing" ? { model_year: "" } : {}),
      ...(kind === "wrong"
        ? { model_year: "2011", campaign_number: "10V039000" }
        : {}),
      ...(kind === "fault"
        ? {
            question:
              "[DEMO_FAIL] 显式模拟生成请求失败，检查运行状态与错误说明。",
          }
        : {}),
    });
    const all = catalog.find((c) => c.release_id === release)?.source_ids || [];
    setSources(
      all.filter((s) =>
        ford ? /17V331|17V652/.test(s) : /10V039|PE10006/.test(s),
      ),
    );
  }
  async function memories() {
    setMemory((await api("/product/memories")).items);
  }
  async function demoMemory() {
    const source = await api("/memory/sources", {
      origin: "user_statement",
      text: "模拟用户偏好：演示输出英文；用于随后撤销练习。",
    });
    const candidate = await api("/memory/candidates", {
      source_id: source.source_id,
      proposal: {
        kind: "preference",
        scope: "user",
        semantic_key: "demo.language",
        value: "演示偏好：输出英文（可撤销）",
      },
    });
    setDetail({ candidate });
  }
  const q = task?.questions?.[0];
  const hits = [
    ...(research?.result?.matched_hits || []),
    ...(research?.result?.pending_hits || []),
  ];
  const records = hits.filter(
    (hit, index) =>
      hits.findIndex(
        (h) =>
          h.source.snapshot_id === hit.source.snapshot_id &&
          h.source.ordinal === hit.source.ordinal,
      ) === index,
  );
  const canWrite =
    task && !["COMPLETED", "CANCELLED", "RUNNING"].includes(task.state);
  return (
    <>
      <header>
        <div>
          <span className="eyebrow">RECALL / EVIDENCE</span>
          <h1>召回证据调查台</h1>
        </div>
        <span className="profile">{info?.profile || "连接服务中"}</span>
      </header>
      <div className="quality-banner" role="note">
        <strong>模型质量验收未获证实</strong>
        <span>预注册顺序与总体语义测量仍有缺口。运行完成不代表结论正确。</span>
        {info?.is_simulated_generation && (
          <b>当前为模拟生成 · 外部模型调用 0</b>
        )}
      </div>
      <main>
        <aside>
          <section>
            <h2>连接本机服务</h2>
            <label>
              本地访问凭据
              <input
                aria-label="本地访问凭据"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                autoComplete="off"
              />
            </label>
            <button
              disabled={busy || !token.trim()}
              onClick={() => run(connect)}
            >
              连接
            </button>
            <p className="muted">
              从启动生成的本地会话文件读取。不要填模型 API 密钥。
            </p>
          </section>
          <nav>
            {["任务", "新建调查", "记忆"].map((n) => (
              <button
                className={tab === n ? "selected" : ""}
                key={n}
                onClick={() => {
                  setTab(n);
                  if (n === "记忆") run(memories);
                }}
              >
                {n}
              </button>
            ))}
          </nav>
          <section>
            <h2>历史任务</h2>
            <button
              disabled={!catalog.length || busy}
              onClick={() => run(() => refresh())}
            >
              刷新任务
            </button>
            {list.map((t) => (
              <button
                className="task-link"
                key={t.task_id}
                onClick={() =>
                  run(() => {
                    setTab("任务");
                    return select(t.task_id);
                  })
                }
              >
                <strong>{t.task_id}</strong>
                <span>
                  {states[t.state]} · v{t.version}
                </span>
              </button>
            ))}
            <div className="row">
              <button
                disabled={busy || taskOffset.current === 0}
                onClick={() =>
                  run(async () => {
                    taskOffset.current = Math.max(0, taskOffset.current - 30);
                    await refresh();
                  })
                }
              >
                上一页任务
              </button>
              <button
                disabled={busy || list.length < 30}
                onClick={() =>
                  run(async () => {
                    taskOffset.current += 30;
                    await refresh();
                  })
                }
              >
                下一页任务
              </button>
            </div>
          </section>
        </aside>
        <article>
          {error && (
            <div role="alert" className="error">
              {error}
            </div>
          )}
          {notice && (
            <p role="status" className="notice">
              {notice}
            </p>
          )}
          {tab === "新建调查" && (
            <section>
              <h2>新建调查</h2>
              <p>先确定范围与来源。计划阶段只进行资料研究。</p>
              <div className="row">
                {[
                  ["prius", "Prius 示例"],
                  ["ford", "F-150 对照"],
                  ["missing", "缺少年份"],
                  ["wrong", "年份不匹配"],
                  ...(info?.is_simulated_generation
                    ? [["fault", "模拟请求失败"]]
                    : []),
                ].map(([k, n]) => (
                  <button key={k} onClick={() => preset(k)}>
                    {n}
                  </button>
                ))}
              </div>
              <label>
                数据发布版本
                <select
                  value={release}
                  onChange={(e) => {
                    setRelease(e.target.value);
                    setSources(
                      catalog.find((c) => c.release_id === e.target.value)
                        ?.source_ids || [],
                    );
                  }}
                >
                  {catalog.map((c) => (
                    <option key={c.release_id}>{c.release_id}</option>
                  ))}
                </select>
              </label>
              <fieldset>
                <legend>允许来源（仅使用勾选项）</legend>
                {(
                  catalog.find((c) => c.release_id === release)?.source_ids ||
                  []
                ).map((s) => (
                  <label className="check" key={s}>
                    <input
                      type="checkbox"
                      checked={sources.includes(s)}
                      onChange={(e) =>
                        setSources(
                          e.target.checked
                            ? [...sources, s]
                            : sources.filter((x) => x !== s),
                        )
                      }
                    />
                    {s}
                  </label>
                ))}
              </fieldset>
              <div className="grid">
                {[
                  ["make", "品牌"],
                  ["model", "车型"],
                  ["model_year", "车型年"],
                  ["campaign_number", "召回编号"],
                  ["production_date", "生产日期"],
                ].map(([k, n]) => (
                  <label key={k}>
                    {n}
                    <input
                      aria-label={n}
                      value={form[k]}
                      onChange={(e) =>
                        setForm({ ...form, [k]: e.target.value })
                      }
                    />
                  </label>
                ))}
              </div>
              <label>
                调查问题
                <textarea
                  aria-label="调查问题"
                  value={form.question}
                  onChange={(e) =>
                    setForm({ ...form, question: e.target.value })
                  }
                />
              </label>
              <label>
                检索词
                <input
                  value={form.query}
                  onChange={(e) => setForm({ ...form, query: e.target.value })}
                />
              </label>
              <div className="grid">
                <label>
                  任务类型
                  <select
                    aria-label="任务类型"
                    value={form.intent}
                    onChange={(e) =>
                      setForm({ ...form, intent: e.target.value })
                    }
                  >
                    {[
                      ["record_lookup", "召回查询"],
                      ["cross_source", "联合调查"],
                      ["relationship", "关系对照"],
                      ["applicability", "个车适用"],
                      ["complaint_statistics", "投诉统计"],
                    ].map(([v, n]) => (
                      <option value={v} key={v}>
                        {n}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  时间模式
                  <select
                    aria-label="时间模式"
                    value={form.time}
                    onChange={(e) => setForm({ ...form, time: e.target.value })}
                  >
                    <option value="snapshot_analysis">冻结快照分析</option>
                    <option value="historical_as_of">
                      当时可知资料（需确认能力边界）
                    </option>
                  </select>
                </label>
              </div>
              {form.time === "historical_as_of" && (
                <label>
                  知识截止时间
                  <input
                    value={form.cutoff}
                    onChange={(e) =>
                      setForm({ ...form, cutoff: e.target.value })
                    }
                  />
                </label>
              )}
              <details>
                <summary>配置与关系条件</summary>
                <label>
                  配置条件 JSON
                  <textarea
                    value={form.configurations}
                    onChange={(e) =>
                      setForm({ ...form, configurations: e.target.value })
                    }
                  />
                </label>
                <label>
                  固定关系记录对 JSON
                  <textarea
                    aria-label="关系记录对"
                    value={form.pairs}
                    onChange={(e) =>
                      setForm({ ...form, pairs: e.target.value })
                    }
                  />
                </label>
                <p>
                  记录对使用范围查询返回的 snapshot_id 与
                  ordinal；不得用相同部件名推定因果或同一缺陷。
                </p>
              </details>
              <button
                className="primary"
                disabled={busy || !sources.length}
                onClick={() => run(submit)}
              >
                创建研究计划
              </button>
            </section>
          )}
          {tab === "任务" && !task && (
            <section className="empty">
              <h2>从证据开始，保留每一步</h2>
              <p>连接后新建调查，或从左侧打开历史任务。</p>
              <button onClick={() => setTab("新建调查")}>新建调查</button>
            </section>
          )}
          {tab === "任务" && task && (
            <>
              <section>
                <div className="section-head">
                  <h2>{task.task_id}</h2>
                  <span className="badge">
                    {states[task.state]} · v{task.version}
                  </span>
                </div>
                <p>{task.plan.request.question}</p>
                <div className="grid">
                  <div className="metric">
                    <small>运行状态</small>
                    <strong>{states[task.state]}</strong>
                    <span>来自服务端实际状态</span>
                  </div>
                  <div className="metric warning">
                    <small>证据质量</small>
                    <strong>
                      {dispositions[task.quality?.disposition] ||
                        "尚未形成答案"}
                    </strong>
                    <span>
                      独立语义核查：
                      {task.quality?.semantic_review === "pending"
                        ? "待审"
                        : "未建立"}
                    </span>
                  </div>
                </div>
                {task.state === "FAILED" && (
                  <p role="alert" className="error">
                    本次执行未完成，没有发布可交付报告。请查看调用与错误记录，检查条件后再恢复；运行失败不代表不存在相关召回。
                  </p>
                )}
                {task.state === "PAUSED" &&
                  dispatch?.jobs?.some((j) => j.last_error) && (
                    <p role="alert" className="error">
                      本次执行已暂停，没有发布可交付报告。上次调用结果未确认，请先查看调用记录并核对；系统不会自动重发未知请求。
                    </p>
                  )}
                <details open>
                  <summary>调查条件与冻结版本</summary>
                  <dl className="facts">
                    <dt>查询车辆</dt>
                    <dd>
                      {task.plan.scope.model_year || "年份待补充"} /{" "}
                      {task.plan.scope.make || "品牌未知"} /{" "}
                      {task.plan.scope.model || "车型未知"}
                    </dd>
                    <dt>召回编号</dt>
                    <dd>
                      {task.plan.scope.campaign_number || "在允许来源中检索"}
                    </dd>
                    <dt>个车条件</dt>
                    <dd>
                      生产日期：{task.plan.scope.production_date || "未提供"}
                      ；配置：
                      {Object.keys(task.plan.scope.configurations).length
                        ? fmt(task.plan.scope.configurations)
                        : "未提供"}
                      。不据此确认个车适用。
                    </dd>
                    <dt>时间范围</dt>
                    <dd>
                      {task.frozen_time.mode === "snapshot_analysis"
                        ? "冻结快照分析"
                        : "历史时点模式"}{" "}
                      ·{" "}
                      {task.frozen_time.knowledge_cutoff || "没有历史时点保证"}
                    </dd>
                    <dt>发布版本</dt>
                    <dd className="digest">{task.plan.release_id}</dd>
                    <dt>允许来源</dt>
                    <dd>{task.plan.allowed_source_ids.join(" / ")}</dd>
                    <dt>未确认条件</dt>
                    <dd>
                      {(
                        report?.proposal?.unresolved_fields ||
                        q?.fields ||
                        []
                      ).join("、") || "无新增待补条件；个车适用性仍未确认"}
                    </dd>
                  </dl>
                </details>
                <details>
                  <summary>计划与授权边界</summary>
                  <Data
                    value={{
                      revision_id: task.revision_id,
                      plan_sha256: task.plan_sha256,
                      plan: task.plan,
                      authorization: task.authorization,
                    }}
                  />
                </details>
                <div className="row">
                  <button
                    disabled={busy || !task.authorization?.active || !canWrite}
                    onClick={() =>
                      run(async () => {
                        setResearch(
                          await api(`/tasks/${task.task_id}/research`, {
                            operation: "search",
                            query: task.plan.request.retrieval_query,
                          }),
                        );
                      })
                    }
                  >
                    查询冻结资料
                  </button>
                  <button
                    disabled={busy || !canWrite || task.plan.mode === "EXECUTE"}
                    onClick={() =>
                      run(() =>
                        mutate("revisions", {
                          plan: { ...task.plan, mode: "EXECUTE" },
                        }),
                      )
                    }
                  >
                    提出执行版本
                  </button>
                  <button
                    className="primary"
                    disabled={
                      busy ||
                      task.state !== "DRAFT" ||
                      task.plan.mode !== "EXECUTE" ||
                      !task.authorization?.active
                    }
                    onClick={() => run(() => mutate("execute"))}
                  >
                    执行已授权计划
                  </button>
                  <button
                    disabled={
                      busy || !["DRAFT", "RUNNING"].includes(task.state)
                    }
                    onClick={() => run(() => mutate("pause"))}
                  >
                    暂停
                  </button>
                  <button
                    disabled={
                      busy || !["PAUSED", "FAILED"].includes(task.state)
                    }
                    onClick={() => run(() => mutate("resume"))}
                  >
                    恢复计划
                  </button>
                  <button
                    className="danger"
                    disabled={
                      busy || ["COMPLETED", "CANCELLED"].includes(task.state)
                    }
                    onClick={() => run(() => mutate("cancel"))}
                  >
                    取消任务
                  </button>
                  <button
                    disabled={busy || !task.authorization?.active}
                    onClick={() => run(() => mutate("revoke"))}
                  >
                    撤销任务授权
                  </button>
                </div>
                <p className="muted">
                  取消为终态；暂停后可恢复。已取消任务可复制为新计划。恢复崩溃任务不自动重发未知的模型请求。
                </p>
                {task.state === "CANCELLED" && (
                  <button
                    onClick={() =>
                      run(async () => {
                        const id =
                          "UI-" + crypto.randomUUID().slice(0, 8).toUpperCase();
                        await api("/tasks", {
                          task_id: id,
                          plan: {
                            ...task.plan,
                            mode: "PLAN",
                            request: { ...task.plan.request, task_id: id },
                          },
                        });
                        await select(id);
                      })
                    }
                  >
                    复制为新计划
                  </button>
                )}
                {q && (
                  <div className="question">
                    <h3>
                      {q.kind === "approval"
                        ? "批准当前具体执行版本"
                        : "补充调查条件"}
                    </h3>
                    <p>{q.prompt}</p>
                    {q.kind === "approval" ? (
                      <>
                        <p>
                          请检查上方版本、来源、范围和执行能力。真实模型模式会产生请求费用。
                        </p>
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(() =>
                              mutate("approvals", {
                                question_id: q.question_id,
                                revision_id: task.revision_id,
                                plan_sha256: task.plan_sha256,
                                approve: true,
                              }),
                            )
                          }
                        >
                          批准此版本
                        </button>
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(() =>
                              mutate("approvals", {
                                question_id: q.question_id,
                                revision_id: task.revision_id,
                                plan_sha256: task.plan_sha256,
                                approve: false,
                              }),
                            )
                          }
                        >
                          拒绝此版本
                        </button>
                      </>
                    ) : (
                      <>
                        {q.fields.map((f) =>
                          f === "accept_frozen_time" ? (
                            <label className="check" key={f}>
                              <input
                                type="checkbox"
                                checked={answers[f] === true}
                                onChange={(e) =>
                                  setAnswers({
                                    ...answers,
                                    [f]: e.target.checked,
                                  })
                                }
                              />
                              接受上方冻结时间，放弃本次历史时点要求
                            </label>
                          ) : (
                            <label key={f}>
                              {f}
                              <input
                                aria-label={`补充 ${f}`}
                                value={answers[f] || ""}
                                onChange={(e) =>
                                  setAnswers({
                                    ...answers,
                                    [f]: e.target.value,
                                  })
                                }
                              />
                            </label>
                          ),
                        )}
                        <button
                          disabled={busy}
                          onClick={() =>
                            run(() =>
                              mutate("answers", {
                                question_id: q.question_id,
                                values: Object.fromEntries(
                                  Object.entries(answers).map(([k, v]) => [
                                    k,
                                    k === "model_year"
                                      ? Number(v)
                                      : k === "configurations"
                                        ? JSON.parse(v)
                                        : v,
                                  ]),
                                ),
                              }),
                            )
                          }
                        >
                          提交澄清
                        </button>
                      </>
                    )}
                  </div>
                )}
                <details>
                  <summary>编辑计划（提交后重新检查授权）</summary>
                  <button onClick={() => setPlanText(fmt(task.plan))}>
                    载入当前计划
                  </button>
                  <textarea
                    aria-label="计划 JSON"
                    className="code"
                    value={planText}
                    onChange={(e) => setPlanText(e.target.value)}
                  />
                  <button
                    disabled={busy || !canWrite || !planText}
                    onClick={() =>
                      run(() =>
                        mutate("revisions", { plan: JSON.parse(planText) }),
                      )
                    }
                  >
                    提交计划修订
                  </button>
                </details>
              </section>
              {research && (
                <section data-testid="research">
                  <h2>冻结资料研究 · 非正式回答</h2>
                  {records.map((h) => (
                    <div className="claim" key={h.document_id}>
                      <h3>
                        {h.metadata?.campaign_number || h.source.source_id} ·{" "}
                        {h.scope_channel}
                      </h3>
                      <p>{h.text}</p>
                      <p className="muted">
                        {h.source.source_id} · {h.field} · 不确认个车适用
                      </p>
                      <button
                        onClick={() =>
                          setPairRecords((old) =>
                            old.some(
                              (r) =>
                                r.snapshot_id === h.source.snapshot_id &&
                                r.ordinal === h.source.ordinal,
                            )
                              ? old.filter(
                                  (r) =>
                                    !(
                                      r.snapshot_id === h.source.snapshot_id &&
                                      r.ordinal === h.source.ordinal
                                    ),
                                )
                              : [
                                  ...old.slice(-1),
                                  {
                                    snapshot_id: h.source.snapshot_id,
                                    ordinal: h.source.ordinal,
                                  },
                                ],
                          )
                        }
                      >
                        {pairRecords.some(
                          (r) =>
                            r.snapshot_id === h.source.snapshot_id &&
                            r.ordinal === h.source.ordinal,
                        )
                          ? "已选入对照"
                          : "选入关系对照"}
                      </button>
                    </div>
                  ))}
                  {pairRecords.length > 0 && (
                    <p>已选择 {pairRecords.length}/2 条原始记录。</p>
                  )}
                  <button
                    disabled={busy || pairRecords.length !== 2 || !canWrite}
                    onClick={() =>
                      run(() =>
                        mutate("revisions", {
                          plan: {
                            ...task.plan,
                            request: {
                              ...task.plan.request,
                              intent: "relationship",
                              pairs: [
                                { left: pairRecords[0], right: pairRecords[1] },
                              ],
                            },
                          },
                        }),
                      )
                    }
                  >
                    固定这两条记录
                  </button>
                  <button
                    disabled={
                      busy || task.plan.request.pairs.length !== 1 || !canWrite
                    }
                    onClick={() =>
                      run(async () => {
                        setResearch(
                          await api(`/tasks/${task.task_id}/research`, {
                            operation: "links",
                          }),
                        );
                      })
                    }
                  >
                    查询已选关系
                  </button>
                  {research.result?.links && (
                    <p>
                      关系只表示结构化规则所支持的联系；请检查条件与未知项，不能直接推定同一缺陷或因果。
                    </p>
                  )}
                  <details>
                    <summary>完整检索或关系结果（含反证与未知项）</summary>
                    <Data value={research} />
                  </details>
                </section>
              )}
              <section>
                <h2>执行进度与回放</h2>
                <p>
                  每 2 秒读取服务端实际事件；没有可观测步骤时不展示虚构百分比。
                </p>
                {dispatch?.jobs?.length ? (
                  dispatch.jobs.map((j) => (
                    <p key={j.job_id}>
                      任务队列：{j.state} · 第 {j.attempt} 次处理{" "}
                      {j.last_error && `· ${j.last_error}`}
                    </p>
                  ))
                ) : (
                  <p className="muted">尚未派发执行。</p>
                )}
                <details>
                  <summary>调用与预算记录</summary>
                  <Data value={dispatch} />
                </details>
                <div className="row">
                  <button
                    disabled={busy}
                    onClick={() =>
                      run(async () =>
                        setHistory(await api(`/tasks/${task.task_id}/history`)),
                      )
                    }
                  >
                    读取执行回放
                  </button>
                  <button
                    disabled={
                      busy || !["PAUSED", "FAILED"].includes(task.state)
                    }
                    onClick={() => run(() => mutate("recover"))}
                  >
                    恢复中断执行
                  </button>
                </div>
                {history && (
                  <>
                    <ol>
                      {history.events.map((e) => (
                        <li key={e.event_id}>
                          v{e.version} · {e.action} ·{" "}
                          {states[e.payload.to_state] || e.payload.to_state}
                        </li>
                      ))}
                    </ol>
                    <details>
                      <summary>完整历史工件</summary>
                      <Data value={history} />
                    </details>
                  </>
                )}
              </section>
              {report && (
                <section data-testid="report">
                  <h2>回答与证据</h2>
                  <p className="notice">
                    {dispositions[report.proposal?.disposition]} ·{" "}
                    {report.generation_mode === "simulated_model"
                      ? "模拟生成"
                      : "真实模型输出，仍需核查"}
                  </p>
                  {report.proposal?.claims?.map((c) => (
                    <div className="claim" key={c.claim_id}>
                      <h3>{c.claim_id}</h3>
                      <p>{c.statement}</p>
                      <div className="row">
                        {c.support_ids.map((a) => (
                          <button key={a} onClick={() => run(() => inspect(a))}>
                            {a} · 支持引用
                          </button>
                        ))}
                        {c.counter_ids.map((a) => (
                          <button key={a} onClick={() => run(() => inspect(a))}>
                            {a} · 反证引用
                          </button>
                        ))}
                      </div>
                    </div>
                  ))}
                  {!report.proposal?.claims?.length && (
                    <p>
                      没有可交付结论。下列证据卡可供检查，不能据此把证据不足改成成功回答。
                    </p>
                  )}
                  {Object.entries(report.aliases || {}).map(([alias, id]) => {
                    const c = report.cards.find((c) => c.evidence_id === id);
                    return (
                      <details className="evidence" key={alias}>
                        <summary>
                          {alias} · {c.kind} · {c.scope_channel}
                        </summary>
                        <p>{c.source_ids.join(" / ")}</p>
                        <p>
                          必需上下文：
                          {c.required_context_ids.length
                            ? c.required_context_ids
                                .map((id) =>
                                  Object.keys(report.aliases).find(
                                    (a) => report.aliases[a] === id,
                                  ),
                                )
                                .join(", ")
                            : "无"}
                        </p>
                        <EvidenceBody card={c} />
                        <button onClick={() => run(() => inspect(alias))}>
                          定位原文 {alias}
                        </button>
                      </details>
                    );
                  })}
                  <details>
                    <summary>完整报告、记忆来源与审计</summary>
                    <Data value={report} />
                  </details>
                </section>
              )}
            </>
          )}
          {tab === "记忆" && (
            <section>
              <h2>可撤销记忆</h2>
              <p>
                记忆用于偏好和资料入口，不能替代官方证据。每次修改绑定当前修订号。
              </p>
              <div className="row">
                <button disabled={busy} onClick={() => run(memories)}>
                  刷新记忆
                </button>
                {info?.is_simulated_generation && (
                  <button disabled={busy} onClick={() => run(demoMemory)}>
                    准备演示偏好候选
                  </button>
                )}
              </div>
              {memory.map((m) => (
                <div className="claim" key={m.memory_id}>
                  <h3>{m.body?.value || m.memory_id}</h3>
                  <p>
                    {m.state} · 修订 {m.revision}
                  </p>
                  <details>
                    <summary>来源与验证记录</summary>
                    <Data value={m} />
                    <button
                      onClick={() =>
                        run(async () =>
                          setDetail(
                            await api(
                              `/product/memories/${m.memory_id}/source`,
                            ),
                          ),
                        )
                      }
                    >
                      查看原始记忆来源
                    </button>
                    <button
                      onClick={() =>
                        run(async () =>
                          setDetail(
                            await api(`/memory/items/${m.memory_id}/history`),
                          ),
                        )
                      }
                    >
                      查看记忆历史
                    </button>
                  </details>
                  <button
                    disabled={busy || m.state !== "active"}
                    onClick={() =>
                      run(async () => {
                        await api(`/memory/items/${m.memory_id}/change`, {
                          expected_revision: m.revision,
                          action: "disable",
                          note: "用户从界面撤销该记忆",
                        });
                        await memories();
                        setDetail(null);
                      })
                    }
                  >
                    停用记忆
                  </button>
                </div>
              ))}
            </section>
          )}
          {detail && (
            <section className="original" data-testid="detail">
              <div className="section-head">
                <h2>
                  {detail.alias
                    ? `原文定位 · ${detail.alias}`
                    : detail.candidate
                      ? "检查演示记忆候选"
                      : "审计详情"}
                </h2>
                <button onClick={() => setDetail(null)}>收起</button>
              </div>
              {detail.page && (
                <>
                  <p className="muted">
                    已重新核对来源与文本完整性；此检查不代表语义正确。
                  </p>
                  <blockquote>{detail.page.text}</blockquote>
                  {detail.original?.locations?.map((l, i) => (
                    <div key={i}>
                      <p>
                        页码：{l.page_number || "结构化资料"} · 字段：
                        {l.field_pointer || l.unit_id}
                      </p>
                      <blockquote>{l.quote}</blockquote>
                    </div>
                  ))}
                </>
              )}
              <details open={!detail.page}>
                <summary>完整审计与定位记录</summary>
                <Data value={detail} />
              </details>
              {detail.page?.next_offset != null && (
                <button
                  onClick={() =>
                    run(async () =>
                      setDetail({
                        ...detail,
                        page: await api(
                          `/tasks/${task.task_id}/context?section=evidence&alias=${detail.alias}&offset=${detail.page.next_offset}&limit=4000`,
                        ),
                      }),
                    )
                  }
                >
                  读取下一段完整原文
                </button>
              )}
              {detail.candidate && (
                <button
                  disabled={busy}
                  onClick={() =>
                    run(async () => {
                      const c = detail.candidate;
                      await api(`/memory/candidates/${c.candidate_id}/review`, {
                        expected_revision: c.revision,
                        proposal_sha256: c.proposal_sha256,
                        decision: "approve",
                        note: "用户明确批准当前演示偏好；用于撤销练习",
                      });
                      await memories();
                      setDetail(null);
                    })
                  }
                >
                  确认保存此演示偏好
                </button>
              )}
            </section>
          )}
        </article>
      </main>
      <footer>
        美国车辆公开召回资料研究 · 固定流程默认 ·
        不确认个车适用、因果关系或永久安全
      </footer>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
