"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  ArrowLeft,
  BookCheck,
  Check,
  ChevronRight,
  ClipboardCopy,
  Download,
  FileCheck2,
  FlaskConical,
  FolderOpen,
  GitPullRequest,
  KeyRound,
  ListChecks,
  LogOut,
  Play,
  Plug,
  Plus,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from "lucide-react";
import type {
  ArtifactRecord,
  AttemptRecord,
  DataPolicy,
  FindingRecord,
  IdentityRecord,
  JobCreate,
  MeResponse,
  Page,
  PatchBundle,
  ProjectRecord,
  RepairRecord,
  Role,
  RunCreate,
  RunLimits,
  RunRecord,
  RunnerRecord,
  ScheduleRecord,
  VersionCreate,
  VersionRecord,
} from "@/types/platform-generated";
import {
  launchKey,
  platform,
  PlatformError,
  uploadArtifact,
} from "@/lib/platform-api";
import { initialValue, SchemaForm, schemaDefinitions } from "./schema-form";
import { ProjectOperations, PublishRepair } from "./project-operations";
import { EvidenceInspector } from "./evidence-inspector";
import { AdvancedReports } from "./advanced-reports";
import { RuntimeOperations } from "./runtime-operations";
import { ResourceQuarantine } from "./resource-quarantine";

type List<T> = Omit<Page, "items"> & { items: T[] };
type Member = IdentityRecord & { roles: Role[] };
type Section =
  | "Projects"
  | "Connections"
  | "Cases"
  | "Runs"
  | "Simulator"
  | "Findings"
  | "Repairs"
  | "Review"
  | "Settings";
const navigation = [
  ["Projects", FolderOpen],
  ["Connections", Plug],
  ["Cases", ListChecks],
  ["Runs", Activity],
  ["Simulator", FlaskConical],
  ["Findings", AlertCircle],
  ["Repairs", GitPullRequest],
  ["Review", BookCheck],
  ["Settings", Settings],
] as const;
const kindNames: Record<string, string> = {
  connection: "Connection",
  requirement: "Requirement",
  check: "Check",
  fixture: "Fixture",
  case: "Case",
  suite: "Suite",
  environment: "Environment",
  fidelity: "Fidelity",
  grader: "Grader",
  source: "Source",
  voice: "Voice",
  probe: "Probe",
  collision: "Collision",
};
const jobNames = [
  "preflight",
  "build",
  "source_import",
  "action_comparison",
  "fresh_agent_pair",
  "reduce_reproduction",
  "simulator_inspect",
  "simulator_reset",
  "calibration",
  "trace_import",
  "incident_draft",
  "collision",
  "probe",
  "voice",
  "patch_proposal",
] as const;
const schemaName = (kind: string) =>
  `${kind[0].toUpperCase()}${kind.slice(1)}Spec`;
const fmt = (date: string | null) =>
  date ? new Date(date).toLocaleString() : "Not reported";
const statusLabel = (value: unknown) =>
  String(value ?? "Unknown").replaceAll("_", " ");

function Status({ value }: { value: unknown }) {
  const text = String(value ?? "UNKNOWN");
  const style = /PASS|READY_FOR_ACCEPTANCE|approved|published|completed/.test(
    text,
  )
    ? "good"
    : /FAIL|BLOCKED|error|interrupted|failed/.test(text)
      ? "bad"
      : /UNKNOWN|REVIEW|pending|queued|preparing|running/.test(text)
        ? "warn"
        : "";
  return <span className={`ev-status ${style}`}>{statusLabel(value)}</span>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="ev-empty">
      <FolderOpen size={28} />
      {children}
    </div>
  );
}

function RoleSelection() {
  return (
    <fieldset className="ev-fieldset">
      <legend>Project Roles</legend>
      <div className="ev-inline">
        {["viewer", "engineer", "reviewer"].map((role) => (
          <label className="ev-check" key={role}>
            <input
              type="checkbox"
              name="role"
              value={role}
              defaultChecked={role === "viewer"}
            />
            {role}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function Details({
  value,
  title = "Technical Details",
}: {
  value: unknown;
  title?: string;
}) {
  return (
    <details className="ev-details">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

function RevisionSelect({
  label,
  kind,
  versions,
  value,
  onChange,
  approved = false,
}: {
  label: string;
  kind: string;
  versions: VersionRecord[];
  value: string;
  onChange: (value: string) => void;
  approved?: boolean;
}) {
  return (
    <label>
      {label}
      <select required value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">Select revision</option>
        {versions
          .filter((v) => v.kind === kind && (!approved || v.approved))
          .map((v) => (
            <option key={v.id} value={v.id}>
              {v.title} · r{v.revision}
              {v.approved ? " · approved" : ""}
            </option>
          ))}
      </select>
    </label>
  );
}

export function EvaluationConsole() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [projectId, setProjectId] = useState("");
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [section, setSection] = useState<Section>("Projects");
  const [versions, setVersions] = useState<VersionRecord[]>([]);
  const [versionCursor, setVersionCursor] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRecord[]>([]);
  const [findings, setFindings] = useState<FindingRecord[]>([]);
  const [repairs, setRepairs] = useState<RepairRecord[]>([]);
  const [schedules, setSchedules] = useState<ScheduleRecord[]>([]);
  const [runners, setRunners] = useState<RunnerRecord[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [search, setSearch] = useState("");
  const [newProject, setNewProject] = useState(false);
  const [workspaceId, setWorkspaceId] = useState("");
  const [newWorkspace, setNewWorkspace] = useState(false);
  const [editing, setEditing] = useState(false);
  const [kind, setKind] = useState("requirement");
  const [draft, setDraft] = useState<unknown>(
    initialValue(schemaDefinitions.RequirementSpec),
  );
  const [logicalId, setLogicalId] = useState("");
  const [title, setTitle] = useState("");
  const [hidden, setHidden] = useState(false);
  const [selectedVersion, setSelectedVersion] = useState<VersionRecord | null>(
    null,
  );
  const [rationale, setRationale] = useState("");
  const [runForm, setRunForm] = useState(false);
  const [suiteId, setSuiteId] = useState("");
  const [connectionId, setConnectionId] = useState("");
  const [environmentId, setEnvironmentId] = useState("");
  const [limits, setLimits] = useState<RunLimits>(
    initialValue(schemaDefinitions.RunLimits) as RunLimits,
  );
  const [repetitions, setRepetitions] = useState(1);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [exportItems, setExportItems] = useState<ArtifactRecord[]>([]);
  const [selectedFinding, setSelectedFinding] = useState<
    | (FindingRecord & {
        evidence: { id: string; kind: string; data: unknown }[];
        reviews: unknown[];
        acceptance?: { state: string };
        check?: VersionRecord;
        requirement?: VersionRecord;
        operational_state?: unknown;
      })
    | null
  >(null);
  const [decision, setDecision] = useState("confirm");
  const [reviewEvidence, setReviewEvidence] = useState<string[]>([]);
  const [waiverExpiry, setWaiverExpiry] = useState("");
  const [selectedRepair, setSelectedRepair] = useState<
    (RepairRecord & { patch: PatchBundle }) | null
  >(null);
  const [jobForm, setJobForm] = useState(false);
  const [repairVersions, setRepairVersions] = useState<string[]>([]);
  const [repairLimits, setRepairLimits] = useState<RunLimits>(() => ({
    ...initialValue(schemaDefinitions.RunLimits) as RunLimits,
    cleanup_reserve: 100, max_reference_calls: 160,
  }));
  const [jobKind, setJobKind] = useState<JobCreate["kind"]>("preflight");
  const [jobVersions, setJobVersions] = useState<string[]>([]);
  const [token, setToken] = useState("");
  const [policy, setPolicy] = useState<DataPolicy | null>(null);
  const [comparison, setComparison] = useState<unknown>(null);
  const [comparisonBase, setComparisonBase] = useState("");
  const [comparisonCandidate, setComparisonCandidate] = useState("");
  const [clock, setClock] = useState(0);
  const refreshEpoch = useRef(0);

  useEffect(() => {
    const update = () => setClock(Date.now());
    update();
    const timer = setInterval(update, 10000);
    return () => clearInterval(timer);
  }, []);

  const refreshProjects = useCallback(async () => {
    const response = await platform<List<ProjectRecord>>("/projects?limit=200");
    setProjects(response.items);
    setProjectId((old) =>
      response.items.some((p) => p.id === old)
        ? old
        : (response.items[0]?.id ?? ""),
    );
  }, []);

  const refresh = useCallback(async () => {
    if (!projectId) return;
    const epoch = ++refreshEpoch.current;
    const [p, v, r, f, patches, intervals] = await Promise.all([
      platform<ProjectRecord>(`/projects/${projectId}`),
      platform<List<VersionRecord>>(
        `/projects/${projectId}/versions?limit=200`,
      ),
      platform<List<RunRecord>>(`/projects/${projectId}/runs?limit=200`),
      platform<List<FindingRecord>>(
        `/projects/${projectId}/findings?limit=200`,
      ),
      platform<List<RepairRecord>>(`/projects/${projectId}/repairs`),
      platform<List<ScheduleRecord>>(`/projects/${projectId}/schedules`),
    ]);
    let people: Member[] = [];
    let agents: RunnerRecord[] = [];
    if (p.roles.includes("owner")) {
      const [membersPage, runnersPage] = await Promise.all([
        platform<List<Member>>(`/workspaces/${p.workspace_id}/members`),
        platform<List<RunnerRecord>>(`/projects/${projectId}/runners`),
      ]);
      people = membersPage.items;
      agents = runnersPage.items;
    }
    if (epoch !== refreshEpoch.current) return;
    setProject(p);
    setPolicy(p.policy);
    setVersions(v.items);
    setVersionCursor(v.next_cursor);
    setRuns(r.items);
    setFindings(f.items);
    setRepairs(patches.items);
    setSchedules(intervals.items);
    setMembers(people);
    setRunners(agents);
  }, [projectId]);

  useEffect(() => {
    let active = true;
    platform<MeResponse>("/me")
      .then(async (data) => {
        if (!active) return;
        setMe(data);
        setWorkspaceId(data.workspaces[0]?.id ?? "");
        await refreshProjects();
      })
      .catch((e) => {
        if (active && (!(e instanceof PlatformError) || e.status !== 401))
          setError(String(e.message));
      })
      .finally(() => {
        if (active) setInitializing(false);
      });
    return () => {
      active = false;
    };
  }, [refreshProjects]);

  useEffect(() => {
    if (!me || !projectId) return;
    let active = true;
    const load = async () => {
      setLoading(true);
      try {
        await refresh();
      } catch (e) {
        if (active) setError((e as Error).message);
      } finally {
        if (active) setLoading(false);
      }
    };
    void load();
    return () => {
      active = false;
      refreshEpoch.current += 1;
    };
  }, [me, projectId, refresh]);

  useEffect(() => {
    const running =
      selectedRun && ["queued", "running"].includes(selectedRun.status);
    if (!running && !exportItems.some((item) => item.status === "pending"))
      return;
    let active = true;
    let fetching = false;
    const timer = setInterval(async () => {
      if (document.hidden || fetching) return;
      fetching = true;
      try {
        let row: RunRecord | null = null;
        let refreshed: ArtifactRecord[] | null = null;
        if (selectedRun && ["queued", "running"].includes(selectedRun.status)) {
          row = await platform<RunRecord>(`/runs/${selectedRun.id}`);
        }
        if (exportItems.some((item) => item.status === "pending")) {
          refreshed = await Promise.all(
            exportItems.map((item) =>
              platform<ArtifactRecord>(`/artifacts/${item.id}`),
            ),
          );
        }
        if (active && row) setSelectedRun(row);
        if (active && refreshed) setExportItems(refreshed);
      } catch (e) {
        if (active) setError((e as Error).message);
      } finally {
        fetching = false;
      }
    }, 2000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [selectedRun, exportItems]);

  const act = async (operation: () => Promise<void>, reload = true) => {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await operation();
      if (me && reload) await refresh();
    } catch (e) {
      setError(
        `${(e as Error).message}${e instanceof PlatformError && e.correlationId ? ` (${e.correlationId})` : ""}`,
      );
    } finally {
      setBusy(false);
    }
  };
  const engineer = Boolean(
    project?.id === projectId && project.roles.includes("engineer"),
  );
  const reviewer = Boolean(
    project?.id === projectId && project.roles.includes("reviewer"),
  );
  const owner = Boolean(
    project?.id === projectId && project.roles.includes("owner"),
  );
  const activeRunners = runners.filter(
    (r) =>
      !r.revoked_at &&
      r.last_heartbeat &&
      clock - new Date(r.last_heartbeat).getTime() < 30000,
  );
  const changeSection = (value: Section) => {
    setSection(value);
    setSelectedRun(null);
    setSelectedVersion(null);
    setSelectedFinding(null);
    setReviewEvidence([]);
    setSelectedRepair(null);
    setEditing(false);
    setRunForm(false);
    setJobForm(false);
    setToken("");
    setError("");
  };
  const openEditor = (value = "requirement", row?: VersionRecord) => {
    setKind(value);
    setDraft(row?.data ?? initialValue(schemaDefinitions[schemaName(value)]));
    setLogicalId(row?.logical_id ?? "");
    setTitle(row?.title ?? "");
    setHidden(row?.hidden ?? false);
    setEditing(true);
    setSelectedVersion(null);
  };
  const runBody = (): RunCreate => ({
    schema_version: "1.0",
    kind: "outcome_suite",
    suite_version_id: suiteId,
    connection_version_id: connectionId,
    environment_version_id: environmentId,
    repetitions,
    limits,
  });
  const openRun = async (row: RunRecord) => {
    setSelectedRun(await platform<RunRecord>(`/runs/${row.id}`));
    setExportItems([]);
  };
  const selectedKinds =
    section === "Connections"
      ? ["connection", "source"]
      : section === "Cases"
        ? ["requirement", "check", "fixture", "case", "suite", "grader"]
        : section === "Simulator"
          ? ["environment", "fidelity", "probe", "collision", "voice"]
          : section === "Review"
            ? Object.keys(kindNames)
            : [];
  const visibleVersions = versions.filter(
    (v) =>
      selectedKinds.includes(v.kind) &&
      `${v.title} ${v.logical_id}`
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (section !== "Review" || !v.approved),
  );

  if (initializing)
    return (
      <div className="evaluation">
        <div className="ev-login">
          <ShieldCheck size={32} />
          <h2>Susanoo</h2>
          <p role="status">Connecting to your workspace...</p>
        </div>
      </div>
    );
  if (!me)
    return (
      <div className="evaluation">
        <div className="ev-top">
          <div className="ev-brand">
            <ShieldCheck size={30} />
            <div>
              <h1>Susanoo</h1>
              <span>Evaluation Workspace</span>
            </div>
          </div>
        </div>
        <div className="ev-login">
          <h2>Sign In</h2>
          {error && (
            <div role="alert" className="ev-error">
              {error}
            </div>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const form = new FormData(e.currentTarget);
              void act(async () => {
                await platform("/auth/login", "POST", {
                  email: form.get("email"),
                  password: form.get("password"),
                });
                const identity = await platform<MeResponse>("/me");
                setMe(identity);
                setWorkspaceId(identity.workspaces[0]?.id ?? "");
                await refreshProjects();
              });
            }}
          >
            <label>
              Email
              <input
                name="email"
                type="email"
                required
                autoComplete="username"
              />
            </label>
            <label>
              Password
              <input
                name="password"
                type="password"
                required
                autoComplete="current-password"
              />
            </label>
            <button className="ev-primary" disabled={busy}>
              <KeyRound size={16} />
              {busy ? "Signing In..." : "Sign In"}
            </button>
          </form>
          <div className="ev-login-links">
            <a href="/evaluation/reset">Reset Password</a>
            <a href="/projects">Legacy App</a>
          </div>
        </div>
      </div>
    );

  return (
    <div className="evaluation">
      <header className="ev-top">
        <div className="ev-brand">
          <ShieldCheck size={30} />
          <div>
            <h1>Susanoo</h1>
            <span>Evaluation Workspace</span>
          </div>
        </div>
        <div className="ev-top-actions">
          <select
            aria-label="Active project"
            value={projectId}
            onChange={(e) => {
              setProjectId(e.target.value);
              setSelectedRun(null);
              setSelectedVersion(null);
              setSelectedFinding(null);
              setSelectedRepair(null);
            }}
          >
            <option value="">Select project</option>
            {projects.map((p) => (
              <option value={p.id} key={p.id}>
                {p.name}
              </option>
            ))}
          </select>
          <small>{me.user.email}</small>
          <button
            className="ev-icon"
            title="Sign out"
            aria-label="Sign out"
            onClick={() =>
              void act(async () => {
                await platform("/auth/logout", "POST");
                setMe(null);
                setToken("");
                setProjects([]);
                setProject(null);
                setProjectId("");
                setSelectedRun(null);
                setExportItems([]);
              }, false)
            }
          >
            <LogOut size={17} />
          </button>
        </div>
      </header>
      <div className="ev-mobile-nav">
        <select
          aria-label="Workspace navigation"
          value={section}
          onChange={(e) => changeSection(e.target.value as Section)}
        >
          {navigation.map(([name]) => (
            <option key={name}>{name}</option>
          ))}
        </select>
      </div>
      <div className="ev-layout">
        <nav className="ev-nav" aria-label="Workspace navigation">
          {navigation.map(([name, Icon]) => (
            <button
              key={name}
              aria-current={section === name ? "page" : undefined}
              onClick={() => changeSection(name)}
            >
              <Icon size={18} />
              {name}
            </button>
          ))}
        </nav>
        <main className="ev-main">
          <div className="ev-page-heading">
            <div>
              <div className="ev-eyebrow">{project?.name ?? "Workspace"}</div>
              <h2>
                {selectedRun
                  ? "Run Evidence"
                  : selectedFinding
                    ? "Finding Review"
                    : selectedRepair
                      ? "Repair Proposal"
                      : selectedVersion
                        ? "Revision Detail"
                        : section}
              </h2>
            </div>
            <div className="ev-toolbar">
              {(selectedRun ||
                selectedFinding ||
                selectedRepair ||
                selectedVersion) && (
                <button
                  className="ev-icon"
                  title="Back to list"
                  aria-label="Back to list"
                  onClick={() => {
                    setSelectedRun(null);
                    setSelectedFinding(null);
                    setSelectedRepair(null);
                    setSelectedVersion(null);
                  }}
                >
                  <ArrowLeft size={17} />
                </button>
              )}
              <button
                className="ev-icon"
                title="Refresh workspace"
                aria-label="Refresh workspace"
                disabled={busy || loading}
                onClick={() => void act(refresh)}
              >
                <RefreshCw size={17} />
              </button>
              {projectId && selectedKinds.length > 0 && !selectedVersion && (
                <button
                  className="ev-primary"
                  disabled={!engineer || busy}
                  onClick={() => openEditor(selectedKinds[0])}
                >
                  <Plus size={16} />
                  New Revision
                </button>
              )}
              {section === "Runs" && !selectedRun && (
                <button
                  className="ev-primary"
                  disabled={!engineer || busy}
                  onClick={() => setRunForm(!runForm)}
                >
                  <Play size={16} />
                  Launch Suite
                </button>
              )}
            </div>
          </div>
          {error && (
            <div className="ev-error" role="alert">
              <span>{error}</span>
              <button
                className="ev-icon"
                aria-label="Dismiss error"
                title="Dismiss error"
                onClick={() => setError("")}
              >
                <X size={15} />
              </button>
            </div>
          )}
          {success && (
            <div className="ev-success" role="status">
              {success}
            </div>
          )}
          {projectId && owner && activeRunners.length === 0 && (
            <div className="ev-notice">
              No customer runner connected. Execution jobs remain queued until a
              compatible runner claims them.
            </div>
          )}
          {loading && (
            <p className="ev-muted" role="status">
              Loading workspace records...
            </p>
          )}

          {section === "Projects" && (
            <>
              <div className="ev-toolbar">
                <button
                  className="ev-primary"
                  onClick={() => setNewProject(!newProject)}
                >
                  <Plus size={16} />
                  New Project
                </button>
                <button
                  className="ev-secondary"
                  onClick={() => setNewWorkspace(!newWorkspace)}
                >
                  <Plus size={16} />
                  New Workspace
                </button>
              </div>
              {newWorkspace && (
                <form
                  className="ev-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const form = new FormData(e.currentTarget);
                    void act(async () => {
                      await platform("/workspaces", "POST", {
                        name: form.get("name"),
                      });
                      const identity = await platform<MeResponse>("/me");
                      setMe(identity);
                      setWorkspaceId(identity.workspaces.at(-1)?.id ?? "");
                      setNewWorkspace(false);
                    });
                  }}
                >
                  <label>
                    Workspace Name
                    <input name="name" required />
                  </label>
                  <div className="ev-form-actions">
                    <button className="ev-primary" disabled={busy}>
                      <Plus size={16} />
                      Create Workspace
                    </button>
                  </div>
                </form>
              )}
              {newProject && (
                <form
                  className="ev-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    const form = new FormData(e.currentTarget);
                    void act(async () => {
                      const created = await platform<ProjectRecord>(
                        "/projects",
                        "POST",
                        {
                          workspace_id: workspaceId,
                          name: form.get("name"),
                          workflow: form.get("workflow"),
                          timezone: form.get("timezone"),
                        },
                      );
                      await refreshProjects();
                      setProjectId(created.id);
                      setNewProject(false);
                    });
                  }}
                >
                  <div className="ev-form-grid">
                    <label>
                      Workspace
                      <select
                        value={workspaceId}
                        onChange={(e) => setWorkspaceId(e.target.value)}
                        required
                      >
                        <option value="">Select workspace</option>
                        {me.workspaces
                          .filter((w) => w.roles.includes("owner"))
                          .map((w) => (
                            <option key={w.id} value={w.id}>
                              {w.name}
                            </option>
                          ))}
                      </select>
                    </label>
                    <label>
                      Project Name
                      <input name="name" required />
                    </label>
                    <label>
                      Business Workflow
                      <input name="workflow" required />
                    </label>
                    <label>
                      Business Timezone
                      <input
                        name="timezone"
                        defaultValue="Asia/Kolkata"
                        required
                      />
                    </label>
                  </div>
                  <div className="ev-form-actions">
                    <button className="ev-primary" disabled={busy}>
                      <Plus size={16} />
                      Create Project
                    </button>
                  </div>
                </form>
              )}
              <h3>Projects</h3>
              {projects.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Project</th>
                        <th>Workflow</th>
                        <th>Timezone</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {projects.map((p) => (
                        <tr key={p.id}>
                          <td>
                            <button
                              className="ev-row-title"
                              onClick={() => {
                                setProjectId(p.id);
                                changeSection("Connections");
                              }}
                            >
                              {p.name}
                            </button>
                          </td>
                          <td>{p.workflow}</td>
                          <td>{p.timezone}</td>
                          <td>
                            <ChevronRight size={15} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No projects yet.</Empty>
              )}
            </>
          )}

          {!projectId && section !== "Projects" && (
            <Empty>No active project.</Empty>
          )}

          {projectId && editing && (
            <form
              className="ev-form"
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  const body: VersionCreate = {
                    logical_id: logicalId,
                    title,
                    data: draft as VersionCreate["data"],
                    hidden,
                    provenance: { source: "manual", author: me.user.id },
                  };
                  const saved = await platform<VersionRecord>(
                    `/projects/${projectId}/versions`,
                    "POST",
                    body,
                  );
                  setEditing(false);
                  setSuccess(`Revision ${saved.revision} saved.`);
                });
              }}
            >
              <div className="ev-form-grid">
                <label>
                  Kind
                  <select
                    value={kind}
                    onChange={(e) => {
                      setKind(e.target.value);
                      setDraft(
                        initialValue(
                          schemaDefinitions[schemaName(e.target.value)],
                        ),
                      );
                    }}
                  >
                    {Object.entries(kindNames).map(([key, name]) => (
                      <option key={key} value={key}>
                        {name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  Logical ID
                  <input
                    required
                    value={logicalId}
                    pattern="[A-Za-z0-9][A-Za-z0-9_.-]*"
                    onChange={(e) => setLogicalId(e.target.value)}
                  />
                </label>
                <label>
                  Title
                  <input
                    required
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </label>
                <label className="ev-check">
                  <input
                    type="checkbox"
                    checked={hidden}
                    disabled={!project?.hidden_access}
                    onChange={(e) => setHidden(e.target.checked)}
                  />
                  Hidden Evaluation
                </label>
              </div>
              <fieldset className="ev-fieldset">
                <legend>{kindNames[kind]}</legend>
                <SchemaForm
                  root
                  name={kind}
                  schema={schemaDefinitions[schemaName(kind)]}
                  value={draft}
                  versions={versions}
                  onChange={setDraft}
                />
              </fieldset>
              <div className="ev-form-actions">
                <button
                  type="button"
                  className="ev-secondary"
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </button>
                <button className="ev-primary" disabled={busy}>
                  <Check size={16} />
                  Save Revision
                </button>
              </div>
            </form>
          )}

          {projectId && selectedKinds.length > 0 && !selectedVersion && (
            <>
              <div className="ev-toolbar" style={{ marginBottom: 16 }}>
                <Search size={17} />
                <input
                  aria-label="Search revisions"
                  value={search}
                  placeholder="Search revisions"
                  onChange={(e) => setSearch(e.target.value)}
                />
                <button
                  className="ev-secondary"
                  disabled={!engineer}
                  onClick={() => {
                    setJobForm(!jobForm);
                    setJobKind(
                      section === "Simulator"
                        ? "action_comparison"
                        : "preflight",
                    );
                  }}
                >
                  <Play size={15} />
                  Run Job
                </button>
              </div>
              {visibleVersions.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Revision</th>
                        <th>Kind</th>
                        <th>Version</th>
                        <th>Approval</th>
                        <th>Visibility</th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleVersions.map((v) => (
                        <tr key={v.id}>
                          <td>
                            <button
                              className="ev-row-title"
                              onClick={() => {
                                setSelectedVersion(v);
                                setRationale("");
                              }}
                            >
                              {v.title}
                            </button>
                            <div className="ev-muted">{v.logical_id}</div>
                          </td>
                          <td>{kindNames[v.kind] ?? v.kind}</td>
                          <td>r{v.revision}</td>
                          <td>
                            <Status
                              value={v.approved ? "approved" : "pending_review"}
                            />
                          </td>
                          <td>{v.hidden ? "Hidden" : "Project"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>
                  {section === "Review"
                    ? "No pending revisions in your authorized view."
                    : "No revisions yet."}
                </Empty>
              )}
              {versionCursor && (
                <button
                  className="ev-secondary"
                  style={{ marginTop: 16 }}
                  onClick={() =>
                    void act(async () => {
                      const page = await platform<List<VersionRecord>>(
                        `/projects/${projectId}/versions?limit=200&after=${versionCursor}`,
                      );
                      setVersions((old) => [...old, ...page.items]);
                      setVersionCursor(page.next_cursor);
                    }, false)
                  }
                >
                  Load More Revisions
                </button>
              )}
            </>
          )}

          {selectedVersion && (
            <section>
              <div className="ev-inline">
                <h3>{selectedVersion.title}</h3>
                <Status
                  value={
                    selectedVersion.approved ? "approved" : "pending_review"
                  }
                />
                <span className="ev-muted">r{selectedVersion.revision}</span>
              </div>
              <Details
                value={selectedVersion.data}
                title="Versioned Configuration"
              />
              <Details
                value={{
                  content_hash: selectedVersion.content_hash,
                  source: selectedVersion.provenance,
                  author: selectedVersion.created_by,
                  created_at: selectedVersion.created_at,
                }}
                title="Provenance and Integrity"
              />
              <form
                className="ev-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    await platform(
                      `/versions/${selectedVersion.id}/approve`,
                      "POST",
                      { content_hash: selectedVersion.content_hash, rationale },
                    );
                    setSelectedVersion({ ...selectedVersion, approved: true });
                    setSuccess("Exact revision approved.");
                  });
                }}
              >
                <label>
                  Review Rationale
                  <textarea
                    required
                    rows={3}
                    value={rationale}
                    onChange={(e) => setRationale(e.target.value)}
                  />
                </label>
                <div className="ev-form-actions">
                  <button
                    type="button"
                    className="ev-secondary"
                    disabled={!engineer}
                    onClick={() =>
                      openEditor(selectedVersion.kind, selectedVersion)
                    }
                  >
                    <Plus size={15} />
                    New Revision
                  </button>
                  <button
                    className="ev-primary"
                    disabled={
                      busy ||
                      !reviewer ||
                      selectedVersion.approved ||
                      selectedVersion.created_by === me.user.id
                    }
                    title={
                      !reviewer
                        ? "Reviewer role required"
                        : "Approve exact content hash"
                    }
                  >
                    <Check size={16} />
                    Approve Revision
                  </button>
                </div>
              </form>
            </section>
          )}

          {projectId && jobForm && (
            <form
              className="ev-form"
              onSubmit={(e) => {
                e.preventDefault();
                const form = new FormData(e.currentTarget);
                void act(async () => {
                  const file = form.get("job-file") as File | null;
                  const artifactIds = file?.size
                    ? [
                        await uploadArtifact(
                          projectId,
                          file,
                          "project",
                          String(form.get("file-purpose")),
                        ),
                      ]
                    : [];
                  const row = await platform<RunRecord>(
                    `/projects/${projectId}/jobs`,
                    "POST",
                    {
                      kind: jobKind,
                      version_ids: jobVersions,
                      input_artifact_ids: artifactIds,
                      limits,
                    },
                    launchKey(),
                  );
                  setJobForm(false);
                  setSection("Runs");
                  await openRun(row);
                });
              }}
            >
              <h3>Execution Job</h3>
              <div className="ev-form-grid">
                <label>
                  Optional Input File
                  <input
                    name="job-file"
                    type="file"
                    accept=".json,.jsonl,.csv,.zip,.tar,.gz,.tgz"
                  />
                </label>
                <label>
                  File Classification
                  <select name="file-purpose">
                    <option value="evidence">Redacted Evidence</option>
                    <option value="source">Approved Source Archive</option>
                  </select>
                </label>
              </div>
              <label>
                Job Type
                <select
                  value={jobKind}
                  onChange={(e) =>
                    setJobKind(e.target.value as JobCreate["kind"])
                  }
                >
                  {jobNames.map((name) => (
                    <option key={name} value={name}>
                      {statusLabel(name)}
                    </option>
                  ))}
                </select>
              </label>
              <fieldset className="ev-fieldset">
                <legend>Pinned Inputs</legend>
                <div className="ev-option-list">
                  {versions.map((v) => (
                    <label className="ev-check" key={v.id}>
                      <input
                        type="checkbox"
                        checked={jobVersions.includes(v.id)}
                        onChange={(e) =>
                          setJobVersions((old) =>
                            e.target.checked
                              ? [...old, v.id]
                              : old.filter((id) => id !== v.id),
                          )
                        }
                      />
                      {v.title} · {v.kind} · r{v.revision}
                    </label>
                  ))}
                </div>
              </fieldset>
              <SchemaForm
                root
                name="limits"
                schema={schemaDefinitions.RunLimits}
                value={limits}
                onChange={(value) => setLimits(value as RunLimits)}
              />
              <div className="ev-form-actions">
                <button
                  type="button"
                  className="ev-secondary"
                  onClick={() => setJobForm(false)}
                >
                  Cancel
                </button>
                <button
                  className="ev-primary"
                  disabled={busy || !jobVersions.length}
                >
                  <Play size={15} />
                  Queue Job
                </button>
              </div>
            </form>
          )}

          {runForm && (
            <form
              className="ev-form"
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  const row = await platform<RunRecord>(
                    `/projects/${projectId}/runs`,
                    "POST",
                    runBody(),
                    launchKey(),
                  );
                  setRunForm(false);
                  await openRun(row);
                });
              }}
            >
              <div className="ev-form-grid">
                <RevisionSelect
                  label="Approved Suite"
                  kind="suite"
                  approved
                  versions={versions}
                  value={suiteId}
                  onChange={setSuiteId}
                />
                <RevisionSelect
                  label="Agent Connection"
                  kind="connection"
                  versions={versions}
                  value={connectionId}
                  onChange={setConnectionId}
                />
                <RevisionSelect
                  label="Approved Environment"
                  kind="environment"
                  approved
                  versions={versions}
                  value={environmentId}
                  onChange={setEnvironmentId}
                />
                <label>
                  Repetitions
                  <input
                    type="number"
                    min={1}
                    max={10}
                    value={repetitions}
                    onChange={(e) => setRepetitions(Number(e.target.value))}
                  />
                </label>
              </div>
              <fieldset className="ev-fieldset">
                <legend>Execution Limits</legend>
                <SchemaForm
                  root
                  name="limits"
                  schema={schemaDefinitions.RunLimits}
                  value={limits}
                  onChange={(value) => setLimits(value as RunLimits)}
                />
              </fieldset>
              <div className="ev-form-actions">
                <button
                  type="button"
                  className="ev-secondary"
                  onClick={() => setRunForm(false)}
                >
                  Cancel
                </button>
                <button
                  className="ev-primary"
                  disabled={busy || !suiteId || !connectionId || !environmentId}
                >
                  <Play size={16} />
                  Launch Approved Suite
                </button>
              </div>
            </form>
          )}

          {section === "Runs" && !selectedRun && (
            <>
              {runs.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Run</th>
                        <th>Type</th>
                        <th>Execution</th>
                        <th>Gate</th>
                        <th>Cases</th>
                        <th>Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runs.map((r) => (
                        <tr key={r.id}>
                          <td>
                            <button
                              className="ev-row-title"
                              onClick={() => void act(() => openRun(r))}
                            >
                              {r.id.slice(0, 8)}
                            </button>
                          </td>
                          <td>{statusLabel(r.kind)}</td>
                          <td>
                            <Status value={r.status} />
                          </td>
                          <td>
                            <Status value={r.gate} />
                          </td>
                          <td>
                            {r.counts.completed}/{r.counts.scheduled}
                          </td>
                          <td>{fmt(r.created_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No evaluation runs yet.</Empty>
              )}
              <h3>Compare Runs</h3>
              <form
                className="ev-inline"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () =>
                    setComparison(
                      await platform("/comparisons", "POST", {
                        baseline_id: comparisonBase,
                        candidate_id: comparisonCandidate,
                      }),
                    ),
                  );
                }}
              >
                <label>
                  Baseline
                  <select
                    required
                    value={comparisonBase}
                    onChange={(e) => setComparisonBase(e.target.value)}
                  >
                    <option value="">Select baseline</option>
                    {runs
                      .filter((r) => r.kind === "outcome_suite")
                      .map((r) => (
                        <option value={r.id} key={r.id}>
                          {r.id.slice(0, 8)} · {r.gate}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  Candidate
                  <select
                    required
                    value={comparisonCandidate}
                    onChange={(e) => setComparisonCandidate(e.target.value)}
                  >
                    <option value="">Select candidate</option>
                    {runs
                      .filter((r) => r.kind === "outcome_suite")
                      .map((r) => (
                        <option value={r.id} key={r.id}>
                          {r.id.slice(0, 8)} · {r.gate}
                        </option>
                      ))}
                  </select>
                </label>
                <button
                  className="ev-secondary"
                  disabled={busy || !comparisonBase || !comparisonCandidate}
                >
                  Compare
                </button>
              </form>
              {comparison !== null && (
                <Details
                  value={comparison}
                  title="Compatibility and Transitions"
                />
              )}
            </>
          )}

          {selectedRun && (
            <section>
              <div className="ev-inline">
                <Status value={selectedRun.gate} />
                <Status value={selectedRun.status} />
                {selectedRun.reproduction_outcome && <Status value={selectedRun.reproduction_outcome} />}
                {selectedRun.original_run_id && <small>Original run {selectedRun.original_run_id.slice(0, 8)}</small>}
                <small>{selectedRun.id}</small>
                <button
                  className="ev-danger"
                  disabled={
                    busy ||
                    ["completed", "interrupted", "cancelled"].includes(
                      selectedRun.status,
                    )
                  }
                  onClick={() =>
                    void act(async () => {
                      await platform(`/runs/${selectedRun.id}/cancel`, "POST");
                      await openRun(selectedRun);
                    })
                  }
                >
                  <Square size={14} />
                  Cancel Run
                </button>
              </div>
              <div className="ev-stats">
                {Object.entries(selectedRun.counts).map(([key, value]) => (
                  <div className="ev-stat" key={key}>
                    <strong>{value}</strong>
                    <span>{statusLabel(key)}</span>
                  </div>
                ))}
              </div>
              {selectedRun.attempts.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Case</th>
                        <th>Execution</th>
                        <th>Business Verdict</th>
                        <th>Cleanup</th>
                        <th>Reconciliation</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedRun.attempts.map((a) => (
                        <tr key={a.id}>
                          <td>
                            {versions.find((v) => v.id === a.case_version_id)
                              ?.title ??
                              a.case_version_id?.slice(0, 8) ??
                              "Job"}
                            <small> #{a.repetition + 1}</small>
                          </td>
                          <td>
                            <Status value={a.execution_state} />
                          </td>
                          <td>
                            <Status value={a.business_verdict} />
                          </td>
                          <td>
                            <Status value={a.cleanup_state} />
                          </td>
                          <td>
                            <Status value={a.reconciliation_state} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No visible attempt details.</Empty>
              )}
              {selectedRun.attempts_next_cursor && (
                <button
                  className="ev-secondary"
                  disabled={busy}
                  onClick={() =>
                    void act(async () => {
                      const page = await platform<List<AttemptRecord>>(
                        `/runs/${selectedRun.id}/attempts?limit=200&after=${selectedRun.attempts_next_cursor}`,
                      );
                      setSelectedRun((old) =>
                        old
                          ? {
                              ...old,
                              attempts: [...old.attempts, ...page.items],
                              attempts_next_cursor: page.next_cursor,
                            }
                          : old,
                      );
                    }, false)
                  }
                >
                  Load More Attempts
                </button>
              )}
              <h3>Evidence Exports</h3>
              <div className="ev-toolbar">
                {["pdf", "json", "junit", "bundle"].map((format) => (
                  <button
                    key={format}
                    className="ev-secondary"
                    disabled={
                      busy ||
                      !["completed", "interrupted", "cancelled"].includes(
                        selectedRun.status,
                      )
                    }
                    onClick={() =>
                      void act(async () => {
                        const item = await platform<ArtifactRecord>(
                          `/runs/${selectedRun.id}/exports`,
                          "POST",
                          {
                            format,
                            audience: "project",
                            snapshot_artifact_id: exportItems[0]?.id,
                          },
                        );
                        setExportItems((old) => [...old, item]);
                      })
                    }
                  >
                    <Download size={15} />
                    {format.toUpperCase()}
                  </button>
                ))}
              </div>
              {exportItems.map((item) => (
                <div
                  className="ev-inline"
                  style={{ marginTop: 12 }}
                  key={item.id}
                >
                  <Status value={item.status} />
                  <span>{item.export_format?.toUpperCase()}</span>
                  {item.status === "ready" && (
                    <a
                      className="ev-secondary"
                      href={`/api/v1/artifacts/${item.id}/download`}
                    >
                      <Download size={15} />
                      Download
                    </a>
                  )}
                </div>
              ))}
              <EvidenceInspector runId={selectedRun.id} key={selectedRun.id} revisionKey={selectedRun.status} />
              <AdvancedReports runId={selectedRun.id} revisionKey={selectedRun.status} reviewer={reviewer} />
              <Details
                value={{
                  manifest_hash: selectedRun.manifest_hash,
                  request: selectedRun.request,
                }}
                title="Pinned Manifest"
              />
            </section>
          )}

          {(section === "Findings" || section === "Review") &&
            !selectedFinding && (
              <>
                <h3>Findings</h3>
                {findings.length ? (
                  <div className="ev-table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Finding</th>
                          <th>Check</th>
                          <th>Verdict</th>
                          <th>Run</th>
                        </tr>
                      </thead>
                      <tbody>
                        {findings.map((f) => (
                          <tr key={f.id}>
                            <td>
                              <button
                                className="ev-row-title"
                                onClick={() =>
                                  void act(async () => {
                                    setSelectedFinding(
                                      await platform(`/findings/${f.id}`),
                                    );
                                    setRationale("");
                                  })
                                }
                              >
                                {f.id.slice(0, 8)}
                              </button>
                            </td>
                            <td>
                              {versions.find((v) => v.id === f.check_version_id)
                                ?.title ?? f.check_version_id.slice(0, 8)}
                            </td>
                            <td>
                              <Status value={f.verdict} />
                            </td>
                            <td>{f.run_id.slice(0, 8)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <Empty>No findings in your authorized view.</Empty>
                )}
              </>
            )}
          {selectedFinding && (
            <section>
              <div className="ev-inline">
                <Status value={selectedFinding.verdict} />
                <Status
                  value={selectedFinding.acceptance?.state ?? "pending"}
                />
                <small>{selectedFinding.id}</small>
                <button
                  className="ev-secondary"
                  disabled={!engineer || busy}
                  onClick={() =>
                    void act(async () => {
                      const run = await platform<RunRecord>(
                        `/findings/${selectedFinding.id}/reproductions`,
                        "POST",
                        undefined,
                        launchKey(),
                      );
                      setSelectedFinding(null);
                      setSection("Runs");
                      await openRun(run);
                    })
                  }
                >
                  <Play size={15} />
                  Fresh Reproduction
                </button>
              </div>
              <Details
                value={selectedFinding.evidence}
                title="Independent Evidence and Check Lineage"
              />
              {selectedFinding.requirement?.data.kind === "requirement" && <div className="ev-form-grid">
                <div><h3>Source Clause</h3><p>{selectedFinding.requirement.data.source_clause}</p></div>
                <div><h3>Required Outcome</h3><p>{selectedFinding.requirement.data.expected}</p></div>
              </div>}
              <Details value={{ check: selectedFinding.check, operations: selectedFinding.operational_state }} title="Check, Scope and Operational State" />
              <Details
                value={selectedFinding.reviews}
                title="Review and Waiver History"
              />
              <form
                className="ev-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    await platform(
                      `/findings/${selectedFinding.id}/reviews`,
                      "POST",
                      { decision, rationale, evidence_ids: reviewEvidence },
                    );
                    setSelectedFinding(
                      await platform(`/findings/${selectedFinding.id}`),
                    );
                    setSuccess("Review appended; machine verdict preserved.");
                  });
                }}
              >
                <fieldset className="ev-fieldset"><legend>Evidence References</legend><div className="ev-option-list">
                  {selectedFinding.evidence.map(item => <label className="ev-check" key={item.id}>
                    <input type="checkbox" checked={reviewEvidence.includes(item.id)} onChange={e => setReviewEvidence(old => e.target.checked ? [...old, item.id] : old.filter(id => id !== item.id))} />{item.kind} · {item.id.slice(0, 8)}
                  </label>)}
                </div></fieldset>
                <div className="ev-form-grid">
                  <label>
                    Decision
                    <select
                      value={decision}
                      onChange={(e) => setDecision(e.target.value)}
                    >
                      {[
                        "confirm",
                        "accept",
                        "dispute",
                        "request_evidence",
                        "intentional_simplification",
                        "classify_defect",
                        "reference_limitation",
                        "revoke_waiver",
                      ].map((d) => (
                        <option key={d} value={d}>
                          {statusLabel(d)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Rationale
                    <textarea
                      required
                      value={rationale}
                      onChange={(e) => setRationale(e.target.value)}
                    />
                  </label>
                </div>
                <div className="ev-form-actions">
                  <button className="ev-primary" disabled={busy || !reviewer || (!reviewEvidence.length && !["request_evidence", "revoke_waiver"].includes(decision))}>
                    <FileCheck2 size={15} />
                    Append Review
                  </button>
                </div>
              </form>
              <form
                className="ev-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void act(async () => {
                    await platform(
                      `/findings/${selectedFinding.id}/waivers`,
                      "POST",
                      {
                        rationale,
                        expires_at: new Date(waiverExpiry).toISOString(),
                      },
                    );
                    setSelectedFinding(
                      await platform(`/findings/${selectedFinding.id}`),
                    );
                    setSuccess(
                      "Scoped waiver recorded; machine verdict unchanged.",
                    );
                  });
                }}
              >
                <div className="ev-inline">
                  <label>
                    Waiver Expiry
                    <input
                      type="datetime-local"
                      required
                      value={waiverExpiry}
                      onChange={(e) => setWaiverExpiry(e.target.value)}
                    />
                  </label>
                  <button
                    className="ev-secondary"
                    disabled={busy || !reviewer || !rationale}
                  >
                    Record Scoped Waiver
                  </button>
                </div>
              </form>
            </section>
          )}

          {section === "Repairs" && !selectedRepair && (
            <>
              <form
                className="ev-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const form = new FormData(e.currentTarget);
                  void act(async () => {
                    const file = form.get("patch") as File;
                    const artifactId = await uploadArtifact(
                      projectId,
                      file,
                      "project",
                      "patch",
                    );
                    await platform(`/projects/${projectId}/repairs`, "POST", {
                      finding_id: form.get("finding"),
                      source_version_id: form.get("source"),
                      patch_artifact_id: artifactId,
                      layer: form.get("layer"),
                    });
                    setSuccess("Patch proposal recorded; validation required.");
                  });
                }}
              >
                <div className="ev-form-grid">
                  <label>
                    Finding
                    <select name="finding" required>
                      <option value="">Select finding</option>
                      {findings.map((f) => (
                        <option key={f.id} value={f.id}>
                          {f.id.slice(0, 8)} · {f.verdict}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Approved Source
                    <select name="source" required>
                      <option value="">Select source revision</option>
                      {versions
                        .filter((v) => v.kind === "source" && v.approved)
                        .map((v) => (
                          <option key={v.id} value={v.id}>
                            {v.title} · r{v.revision}
                          </option>
                        ))}
                    </select>
                  </label>
                  <label>
                    Target Layer
                    <select name="layer">
                      <option value="agent">Agent</option>
                      <option value="simulator">Simulator</option>
                      <option value="evaluator">Evaluator</option>
                    </select>
                  </label>
                  <label>
                    Patch Bundle
                    <input
                      name="patch"
                      type="file"
                      accept="application/json,.json"
                      required
                    />
                  </label>
                </div>
                <div className="ev-form-actions">
                  <button className="ev-primary" disabled={busy || !engineer}>
                    <Plus size={15} />
                    Propose Patch
                  </button>
                </div>
              </form>
              {repairs.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Proposal</th>
                        <th>Layer</th>
                        <th>State</th>
                        <th>Pinned Base</th>
                      </tr>
                    </thead>
                    <tbody>
                      {repairs.map((r) => (
                        <tr key={r.id}>
                          <td>
                            <button
                              className="ev-row-title"
                              onClick={() =>
                                void act(async () =>
                                  setSelectedRepair(
                                    await platform(`/repairs/${r.id}`),
                                  ),
                                )
                              }
                            >
                              {r.id.slice(0, 8)}
                            </button>
                          </td>
                          <td>{r.layer}</td>
                          <td>
                            <Status value={r.state} />
                          </td>
                          <td>{r.base_commit.slice(0, 12)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No repair proposals.</Empty>
              )}
            </>
          )}
          {selectedRepair && (
            <section>
              <label>
                Review Rationale
                <textarea
                  value={rationale}
                  onChange={(e) => setRationale(e.target.value)}
                  maxLength={4000}
                />
              </label>
              <div className="ev-inline">
                <Status value={selectedRepair.state} />
                <span>{selectedRepair.layer}</span>
                <small>Base {selectedRepair.base_commit.slice(0, 12)}</small>
              </div>
              {selectedRepair.patch.files.map((file) => (
                <details className="ev-details" key={file.path} open>
                  <summary>{file.path}</summary>
                  <div className="ev-form-grid">
                    <div>
                      <h3>Before</h3>
                      <pre>{file.before ?? "New file"}</pre>
                    </div>
                    <div>
                      <h3>Proposed</h3>
                      <pre>{file.after ?? "Deleted file"}</pre>
                    </div>
                  </div>
                </details>
              ))}
              <details className="ev-details">
                <summary>Validation Scope And Limits</summary>
                <fieldset>
                  <legend>Additional Approved Revisions</legend>
                  {versions.filter((version) => version.approved && ["suite", "fidelity"].includes(version.kind))
                    .map((version) => <label key={version.id} className="ev-checkbox-row">
                      <input type="checkbox" checked={repairVersions.includes(version.id)}
                        onChange={(event) => setRepairVersions((current) => event.target.checked
                          ? [...current, version.id] : current.filter((id) => id !== version.id))} />
                      {version.title} · {version.kind} · r{version.revision}
                    </label>)}
                </fieldset>
                <SchemaForm root name="limits" schema={schemaDefinitions.RunLimits} value={repairLimits}
                  onChange={(value) => setRepairLimits(value as RunLimits)} />
              </details>
              <div className="ev-toolbar">
                <button
                  className="ev-secondary"
                  disabled={busy || !engineer}
                  onClick={() =>
                    void act(async () => {
                      await platform(
                        `/repairs/${selectedRepair.id}/validate`,
                        "POST",
                        { version_ids: repairVersions, limits: repairLimits },
                        launchKey(),
                      );
                      setSelectedRepair(
                        await platform(`/repairs/${selectedRepair.id}`),
                      );
                    })
                  }
                >
                  <Play size={15} />
                  Validate Patch
                </button>
                <button
                  className="ev-primary"
                  disabled={busy || !reviewer || !rationale.trim() || !selectedRepair.available_validation_digest}
                  onClick={() =>
                    void act(async () => {
                      await platform(
                        `/repairs/${selectedRepair.id}/approve`,
                        "POST",
                        {
                          content_hash: selectedRepair.proposal_hash,
                          evidence_digest: selectedRepair.available_validation_digest,
                          rationale,
                        },
                      );
                      setSelectedRepair(
                        await platform(`/repairs/${selectedRepair.id}`),
                      );
                    })
                  }
                >
                  <Check size={15} />
                  Approve Exact Patch
                </button>
                {selectedRepair.pr_url && (
                  <a
                    className="ev-secondary"
                    href={selectedRepair.pr_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <GitPullRequest size={15} />
                    Draft Pull Request
                  </a>
                )}
              </div>
              <Details
                value={selectedRepair}
                title="Validation and Publication Record"
              />
              <PublishRepair
                projectId={projectId}
                repairId={selectedRepair.id}
                proposalHash={selectedRepair.proposal_hash}
                enabled={
                  reviewer &&
                  ["approved", "publication_pending"].includes(
                    selectedRepair.state,
                  )
                }
                onPublished={async () =>
                  setSelectedRepair(
                    await platform(`/repairs/${selectedRepair.id}`),
                  )
                }
              />
              {selectedRepair.validation_run_id && <AdvancedReports runId={selectedRepair.validation_run_id}
                revisionKey={selectedRepair.state} reviewer={reviewer} />}
            </section>
          )}

          {section === "Settings" && project && (
            <>
              <div className="ev-inline">
                <span>{me.user.email}</span>
                {project.roles.map((role) => (
                  <Status key={role} value={role} />
                ))}
                {project.hidden_access && <Status value="hidden_access" />}
              </div>
              <h3>Data and Execution Policy</h3>
              <form className="ev-form" key={project.id} onSubmit={e => {
                e.preventDefault(); const values = new FormData(e.currentTarget);
                void act(async () => {
                  await platform(`/projects/${project.id}`, "PATCH", { name: values.get("project-name"), workflow: values.get("workflow"), timezone: values.get("timezone") });
                  await refreshProjects();
                });
              }}><div className="ev-form-grid">
                <label>Project Name<input name="project-name" defaultValue={project.name} required maxLength={200} /></label>
                <label>Business Workflow<input name="workflow" defaultValue={project.workflow} required maxLength={200} /></label>
                <label>Business Timezone<input name="timezone" defaultValue={project.timezone} required /></label>
              </div><div className="ev-form-actions"><button className="ev-secondary" disabled={busy || !owner}>Save Project</button></div></form>
              {policy && (
                <form
                  className="ev-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void act(async () => {
                      await platform(
                        `/projects/${projectId}/policy`,
                        "POST",
                        policy,
                      );
                      setSuccess(
                        "New owner-authorized policy revision activated.",
                      );
                    });
                  }}
                >
                  <SchemaForm
                    root
                    name="policy"
                    schema={schemaDefinitions.DataPolicy}
                    value={policy}
                    onChange={(value) => setPolicy(value as DataPolicy)}
                  />
                  <div className="ev-form-actions">
                    <button className="ev-primary" disabled={busy || !owner}>
                      <Check size={15} />
                      Save Policy Revision
                    </button>
                  </div>
                </form>
              )}
              <h3>Runner and CLI Credentials</h3>
              <div className="ev-toolbar">
                <button
                  className="ev-secondary"
                  disabled={busy || !owner}
                  onClick={() =>
                    void act(async () => {
                      const created = await platform<{ token: string }>(
                        `/projects/${projectId}/runner-registration`,
                        "POST",
                      );
                      setToken(created.token);
                    })
                  }
                >
                  <Plus size={15} />
                  Runner Registration
                </button>
                <button
                  className="ev-secondary"
                  disabled={busy || !engineer}
                  onClick={() =>
                    void act(async () => {
                      const created = await platform<{ token: string }>(
                        `/projects/${projectId}/cli-credentials`,
                        "POST",
                      );
                      setToken(created.token);
                    })
                  }
                >
                  <KeyRound size={15} />
                  Scoped CLI Credential
                </button>
              </div>
              {token && (
                <div className="ev-token">
                  <div className="ev-inline">
                    <strong>New Credential</strong>
                    <button
                      className="ev-icon"
                      title="Copy credential"
                      aria-label="Copy credential"
                      onClick={() => void navigator.clipboard.writeText(token)}
                    >
                      <ClipboardCopy size={16} />
                    </button>
                    <button
                      className="ev-icon"
                      title="Hide credential"
                      aria-label="Hide credential"
                      onClick={() => setToken("")}
                    >
                      <X size={16} />
                    </button>
                  </div>
                  <code>{token}</code>
                </div>
              )}
              {runners.length ? (
                <div className="ev-table-wrap" style={{ marginTop: 16 }}>
                  <table>
                    <thead>
                      <tr>
                        <th>Runner</th>
                        <th>Version</th>
                        <th>Last Heartbeat</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {runners.map((r) => (
                        <tr key={r.id}>
                          <td>{r.name}</td>
                          <td>{r.version}</td>
                          <td>
                            {r.revoked_at ? "Revoked" : fmt(r.last_heartbeat)}
                          </td>
                          <td>
                            <button
                              className="ev-icon"
                              disabled={busy || !owner || Boolean(r.revoked_at)}
                              title="Revoke runner"
                              aria-label={`Revoke ${r.name}`}
                              onClick={() =>
                                void act(async () => {
                                  await platform(
                                    `/runners/${r.id}/revoke`,
                                    "POST",
                                  );
                                })
                              }
                            >
                              <Trash2 size={15} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty>No registered runners.</Empty>
              )}
              <h3>Members</h3>
              {owner ? (
                <>
                  <div className="ev-table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Member</th>
                          <th>Workspace Roles</th>
                          <th />
                        </tr>
                      </thead>
                      <tbody>
                        {members.map((member) => (
                          <tr key={member.id}>
                            <td>{member.email}</td>
                            <td>{member.roles.join(", ")}</td>
                            <td>
                              <button
                                className="ev-icon"
                                title="Revoke workspace membership"
                                aria-label={`Revoke membership ${member.email}`}
                                disabled={busy || member.id === me.user.id}
                                onClick={() => {
                                  if (
                                    window.confirm(
                                      `Revoke ${member.email}'s workspace membership?`,
                                    )
                                  )
                                    void act(async () => {
                                      await platform(
                                        `/workspaces/${project.workspace_id}/members/${member.id}`,
                                        "DELETE",
                                      );
                                    });
                                }}
                              >
                                <Trash2 size={15} />
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <form
                    className="ev-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const form = new FormData(e.currentTarget);
                      void act(async () => {
                        await platform(
                          `/workspaces/${project.workspace_id}/invitations`,
                          "POST",
                          {
                            email: form.get("email"),
                            roles: form.getAll("role"),
                            project_id: projectId,
                            hidden_access: form.get("hidden") === "on",
                          },
                        );
                        setSuccess(
                          "Invitation queued for the configured email service.",
                        );
                      });
                    }}
                  >
                    <div className="ev-form-grid">
                      <label>
                        Invite Email
                        <input name="email" type="email" required />
                      </label>
                      <RoleSelection />
                      <label className="ev-check">
                        <input name="hidden" type="checkbox" />
                        Explicit Hidden-Case Grant
                      </label>
                    </div>
                    <div className="ev-form-actions">
                      <button className="ev-secondary" disabled={busy}>
                        <Plus size={15} />
                        Invite Member
                      </button>
                    </div>
                  </form>
                  <form
                    className="ev-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      const form = new FormData(e.currentTarget);
                      void act(async () => {
                        await platform(
                          `/workspaces/${project.workspace_id}/members`,
                          "POST",
                          {
                            user_id: form.get("user"),
                            roles: form.getAll("role"),
                            project_id: projectId,
                            hidden_access: form.get("hidden") === "on",
                          },
                        );
                        setSuccess("Project grant updated.");
                      });
                    }}
                  >
                    <div className="ev-form-grid">
                      <label>
                        Existing Member
                        <select name="user">
                          {members.map((member) => (
                            <option key={member.id} value={member.id}>
                              {member.email}
                            </option>
                          ))}
                        </select>
                      </label>
                      <RoleSelection />
                      <label className="ev-check">
                        <input name="hidden" type="checkbox" />
                        Hidden-Case Access
                      </label>
                    </div>
                    <div className="ev-form-actions">
                      <button className="ev-secondary" disabled={busy}>
                        Update Project Grant
                      </button>
                    </div>
                  </form>
                </>
              ) : (
                <p className="ev-muted">
                  Owner permission is required to manage membership.
                </p>
              )}
              <h3>Schedules</h3>
              {schedules.length ? (
                <div className="ev-table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Interval</th>
                        <th>Next Due</th>
                        <th>State</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {schedules.map((s) => (
                        <tr key={s.id}>
                          <td>{s.name}</td>
                          <td>{s.interval_seconds}s</td>
                          <td>{fmt(s.next_due_at)}</td>
                          <td>
                            <Status
                              value={s.enabled ? "enabled" : "disabled"}
                            />
                          </td>
                          <td>
                            <button
                              className="ev-icon"
                              disabled={busy || !engineer || !s.enabled}
                              title="Disable schedule"
                              aria-label={`Disable ${s.name}`}
                              onClick={() =>
                                void act(async () => {
                                  await platform(
                                    `/schedules/${s.id}`,
                                    "DELETE",
                                  );
                                })
                              }
                            >
                              <Square size={15} />
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="ev-muted">No schedules configured.</p>
              )}
              <form
                className="ev-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  const form = new FormData(e.currentTarget);
                  void act(async () => {
                    await platform(`/projects/${projectId}/schedules`, "POST", {
                      name: form.get("name"),
                      interval_seconds: Number(form.get("interval")),
                      run: runBody(),
                      enabled: form.get("enabled") === "on",
                      automatic_budget: {
                        allowed_suites: [suiteId], max_runs_per_window: Number(form.get("autoRuns")),
                        window_seconds: Number(form.get("autoWindow")), max_cases_per_run: Number(form.get("autoCases")),
                        max_agent_invocations: Number(form.get("autoInvocations")),
                        max_cost_minor: form.get("autoCost") ? Number(form.get("autoCost")) : null,
                      },
                    });
                    setSuccess("Pinned schedule created.");
                  });
                }}
              >
                <div className="ev-form-grid">
                  <label>
                    Name
                    <input name="name" required />
                  </label>
                  <label>
                    Interval Seconds
                    <input
                      name="interval"
                      type="number"
                      defaultValue={3600}
                      min={60}
                      required
                    />
                  </label>
                  <RevisionSelect
                    label="Approved Suite"
                    kind="suite"
                    approved
                    versions={versions}
                    value={suiteId}
                    onChange={setSuiteId}
                  />
                  <RevisionSelect
                    label="Connection"
                    kind="connection"
                    versions={versions}
                    value={connectionId}
                    onChange={setConnectionId}
                  />
                  <RevisionSelect
                    label="Approved Environment"
                    kind="environment"
                    approved
                    versions={versions}
                    value={environmentId}
                    onChange={setEnvironmentId}
                  />
                  <label className="ev-check">
                    <input name="enabled" type="checkbox" />
                    Enabled
                  </label>
                  <label>Automatic Runs per Window<input name="autoRuns" type="number" defaultValue={10}
                    min={0} max={1000} required /></label>
                  <label>Budget Window (seconds)<input name="autoWindow" type="number" defaultValue={3600}
                    min={60} max={2592000} required /></label>
                  <label>Cases per Automatic Run<input name="autoCases" type="number" defaultValue={50}
                    min={1} max={5000} required /></label>
                  <label>Agent Invocation Cap<input name="autoInvocations" type="number" defaultValue={400}
                    min={1} max={100000} required /></label>
                  <label>Known Cost Cap (minor units)<input name="autoCost" type="number" min={0} /></label>
                </div>
                <div className="ev-form-actions">
                  <button className="ev-secondary" disabled={busy || !engineer}>
                    Create Schedule
                  </button>
                </div>
              </form>
              <h3>Governance</h3>
              {(engineer || reviewer || owner) && <RuntimeOperations projectId={projectId} owner={owner} key={projectId} />}
              {reviewer && (
                <ResourceQuarantine projectId={projectId} key={projectId} />
              )}
              <ProjectOperations
                projectId={projectId}
                owner={owner}
                versions={versions}
              />
              <button
                className="ev-danger"
                disabled={busy || !owner}
                onClick={() => {
                  if (
                    window.confirm(
                      `Delete ${project.name}? Access is revoked immediately; hosted payloads will be purged.`,
                    )
                  )
                    void act(async () => {
                      await platform(`/projects/${projectId}`, "DELETE");
                      setProject(null);
                      setProjectId("");
                      await refreshProjects();
                      setSection("Projects");
                    });
                }}
              >
                <Trash2 size={15} />
                Delete Project
              </button>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
