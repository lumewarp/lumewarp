import { useMemo, type ReactNode } from "react";

import type { McpGrant, ModelAccess } from "../api";
import { useA2aAgents, useCan, useMcpToolCatalog } from "../hooks";
import { Checkbox } from "./ui/checkbox";
import { HelpTip } from "./ui/help-tip";
import { Label } from "./ui/label";
import { RadioGroup, RadioGroupItem } from "./ui/radio-group";
import { Skeleton } from "./ui/skeleton";

/**
 * The parent level's grants, which a lower level may only narrow. `null` means every target, so a level under
 * it cannot be listed from the parent alone; a caller that may read the catalog gets the names from there.
 */
export interface TargetScope {
  mcp: McpGrant[] | null;
  agents: string[] | null;
}

const byName = (a: string, b: string) => a.localeCompare(b);
const sortedGrants = (grants: McpGrant[]) => [...grants].sort((a, b) => byName(a.server, b.server));

function Legend({ title, hint }: { title: string; hint: string }) {
  return (
    <legend className="mb-1 text-sm font-semibold">
      <span className="flex items-center gap-1">
        {title}
        <HelpTip label={title} text={hint} />
      </span>
    </legend>
  );
}

/**
 * The level choice: every target, or the ones ticked. A member or service account that names none of its own
 * says "Same as the team" instead of the model picker's "Inherit", so each group's options read on their own
 * (three groups named "All"/"Selected" in one dialog would be ambiguous to a reader and to a screen reader).
 */
function AccessChoice({ legend, what, isTeam, access, onAccess, error }: { legend: string; what: string; isTeam?: boolean; access: ModelAccess; onAccess: (a: ModelAccess) => void; error?: string }) {
  const options = [
    isTeam ? { value: "all", label: `Every ${what}` } : { value: "inherit", label: "Same as the team" },
    { value: "list", label: `Chosen ${what}s` },
  ] as const;
  return (
    <RadioGroup
      value={access === "list" ? "list" : options[0].value}
      onValueChange={(value) => onAccess(value as ModelAccess)}
      aria-label={legend}
      className="flex flex-wrap gap-x-5 gap-y-2"
      aria-invalid={!!error}
    >
      {options.map((option) => (
        <div key={option.value} className="flex items-center gap-2">
          <RadioGroupItem value={option.value} id={`${legend.replaceAll(" ", "-")}-${option.value}`} />
          <Label htmlFor={`${legend.replaceAll(" ", "-")}-${option.value}`} className="cursor-pointer font-normal">
            {option.label}
          </Label>
        </div>
      ))}
    </RadioGroup>
  );
}

/** The message under an empty pick list: why nothing is on offer. */
function Empty({ text }: { text: string }) {
  return <p className="text-sm text-muted-foreground">{text}</p>;
}

/** A name the parent does not offer (or that no longer exists), still ticked so it can be removed. */
function NotOffered({ id, name, reason, onClear }: { id: string; name: string; reason: string; onClear: () => void }) {
  return (
    <div className="flex items-center gap-2">
      <Checkbox id={id} checked onCheckedChange={() => onClear()} />
      <Label htmlFor={id} className="min-w-0 cursor-pointer font-normal [overflow-wrap:anywhere]">
        {name} <span className="text-muted-foreground">{reason}</span>
      </Label>
    </div>
  );
}

function NotOfferedGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div role="group" aria-label={label} className="flex flex-col gap-1.5">
      <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">Not offered</span>
      {children}
    </div>
  );
}

/** One server: in or out, then every tool or the ticked ones. */
function ServerGrant({
  server,
  grant,
  allowAll,
  tools,
  onChange,
}: {
  server: string;
  grant: McpGrant | undefined;
  /** The parent permits every tool of this server (so "All tools" may be chosen). */
  allowAll: boolean;
  /** Tool names that may be ticked: the server's first-seen definitions, plus whatever the parent allows. */
  tools: string[];
  onChange: (next: McpGrant | null) => void;
}) {
  const chosen = grant?.tools ?? [];
  // A tool granted before the parent narrowed, or a definition the control plane no longer carries, stays
  // visible so it can be removed instead of disappearing from the form.
  const stale = chosen.filter((tool) => !tools.includes(tool));
  const toggle = (tool: string, on: boolean) =>
    onChange({ server, all_tools: false, tools: on ? [...new Set([...chosen, tool])].sort(byName) : chosen.filter((name) => name !== tool) });
  return (
    <div role="group" aria-label={server} className="flex flex-col gap-1.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <Checkbox
          id={`mcp-server-${server}`}
          checked={!!grant}
          onCheckedChange={(checked) => onChange(checked === true ? { server, all_tools: allowAll, tools: [] } : null)}
        />
        <Label htmlFor={`mcp-server-${server}`} className="min-w-0 cursor-pointer font-normal [overflow-wrap:anywhere]">
          {server}
        </Label>
        {grant &&
          (allowAll ? (
            <RadioGroup
              value={grant.all_tools ? "all" : "list"}
              onValueChange={(value) => onChange({ ...grant, all_tools: value === "all" })}
              aria-label={`${server} tools`}
              className="ml-auto flex flex-wrap gap-x-3 gap-y-1"
            >
              {[
                { value: "all", label: "All tools" },
                { value: "list", label: "Selected tools" },
              ].map((option) => (
                <div key={option.value} className="flex items-center gap-1.5">
                  <RadioGroupItem value={option.value} id={`mcp-${server}-tools-${option.value}`} />
                  <Label htmlFor={`mcp-${server}-tools-${option.value}`} className="cursor-pointer font-normal">
                    {option.label}
                  </Label>
                </div>
              ))}
            </RadioGroup>
          ) : (
            <span className="ml-auto text-xs text-muted-foreground">The level above names the tools.</span>
          ))}
      </div>
      {grant && !grant.all_tools && (
        <div className="flex flex-col gap-1.5 pl-6">
          {tools.length === 0 && stale.length === 0 && (
            <Empty text={allowAll ? "No tool is known for this server yet: run Discover now on the server page." : "The level above allows no tool on this server."} />
          )}
          {tools.length > 0 && (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {tools.map((tool, index) => (
                <div key={tool} className="flex items-center gap-2">
                  <Checkbox id={`mcp-tool-${server}-${index}`} checked={chosen.includes(tool)} onCheckedChange={(checked) => toggle(tool, checked === true)} />
                  <Label htmlFor={`mcp-tool-${server}-${index}`} className="min-w-0 cursor-pointer font-normal [overflow-wrap:anywhere]">
                    {tool}
                  </Label>
                </div>
              ))}
            </div>
          )}
          {stale.length > 0 && (
            <NotOfferedGroup label={`${server} tools not offered`}>
              {stale.map((tool, index) => (
                <NotOffered
                  key={tool}
                  id={`mcp-tool-stale-${server}-${index}`}
                  name={tool}
                  reason={allowAll ? "(no known definition)" : "(outside the parent's scope)"}
                  onClear={() => toggle(tool, false)}
                />
              ))}
            </NotOfferedGroup>
          )}
          {chosen.length === 0 && tools.length > 0 && <span className="text-xs text-muted-foreground">No tool ticked: this server is not usable at this level.</span>}
        </div>
      )}
    </div>
  );
}

interface McpProps {
  isTeam?: boolean;
  access: ModelAccess;
  onAccess: (access: ModelAccess) => void;
  selected: McpGrant[];
  onSelected: (grants: McpGrant[]) => void;
  /** The parent level's grants; null = every server. */
  scope: McpGrant[] | null;
  error?: string;
}

/**
 * Which MCP servers, and which of their tools, a level may use. The tool choices are the server's known
 * definitions (`kind: tool`, any state) — a grant may only name tools the control plane has seen — and the
 * catalog is read only while a list is chosen and the caller may read the routing catalog at all.
 */
export function McpGrants({ isTeam, access, onAccess, selected, onSelected, scope, error }: McpProps) {
  const perms = useCan();
  // Servers, definitions and agents are the installation-admin routing catalog; a narrower caller works from
  // the parent level's own grants, which are exactly what it may hand down.
  const canReadCatalog = perms.canReadRouting;
  const catalog = useMcpToolCatalog(access === "list" && canReadCatalog);
  const parents = useMemo(
    () => (scope === null ? null : new Map(scope.map((grant) => [grant.server, { all: grant.all_tools, tools: new Set(grant.tools) }]))),
    [scope],
  );
  const offered = useMemo(
    () => (parents ? [...parents.keys()].sort(byName) : [...(catalog.data?.servers ?? [])].sort(byName)),
    [parents, catalog.data],
  );
  const allowAll = (server: string) => !parents || (parents.get(server)?.all ?? true);
  const toolsOf = (server: string) => {
    const parent = parents?.get(server);
    // A parent that names its tools is the whole pick list: nothing above it may be added.
    if (parent && !parent.all) return [...parent.tools].sort(byName);
    const names = new Set(catalog.data?.tools.get(server) ?? []);
    for (const tool of parent?.tools ?? []) names.add(tool);
    return [...names].sort(byName);
  };
  const exists = (server: string) => !catalog.data || catalog.data.servers.includes(server);
  const setGrant = (server: string, next: McpGrant | null) =>
    onSelected(next ? sortedGrants([...selected.filter((grant) => grant.server !== server), next]) : selected.filter((grant) => grant.server !== server));
  const notOffered = selected.filter((grant) => !offered.includes(grant.server));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="mcp-grants">
      <Legend
        title="MCP access"
        hint={
          access === "list"
            ? isTeam
              ? "Only the servers and tools ticked below."
              : "Only the servers and tools ticked below, within what the team allows; nothing ticked denies every server."
            : isTeam
              ? "Every server, including ones added later."
              : "Whatever the team allows."
        }
      />
      <AccessChoice legend="MCP access" what="server" isTeam={isTeam} access={access} onAccess={onAccess} error={error} />
      {access === "list" && (
        <div className="flex max-h-80 flex-col gap-3 overflow-y-auto rounded-md border p-3" data-testid="mcp-grant-options">
          {catalog.isPending && canReadCatalog && <Skeleton className="h-4 w-full" />}
          {catalog.error && (
            <p className="text-[13px] text-destructive" role="alert">
              {catalog.error.message}
            </p>
          )}
          {scope === null && !canReadCatalog && <Empty text="Only an installation admin can list the MCP servers, so this level has no names to narrow." />}
          {!catalog.isPending && offered.length === 0 && notOffered.length === 0 && scope !== null && <Empty text="The parent allows no MCP server." />}
          {!catalog.isPending && offered.length === 0 && notOffered.length === 0 && scope === null && canReadCatalog && <Empty text="No MCP server exists yet." />}
          {offered.map((server) => (
            <ServerGrant
              key={server}
              server={server}
              grant={selected.find((grant) => grant.server === server)}
              allowAll={allowAll(server)}
              tools={toolsOf(server)}
              onChange={(next) => setGrant(server, next)}
            />
          ))}
          {notOffered.length > 0 && (
            <NotOfferedGroup label="MCP servers not offered">
              {notOffered.map((grant) => (
                <NotOffered
                  key={grant.server}
                  id={`mcp-server-stale-${grant.server}`}
                  name={grant.server}
                  reason={exists(grant.server) ? "(outside the parent's scope)" : "(no longer exists)"}
                  onClear={() => setGrant(grant.server, null)}
                />
              ))}
            </NotOfferedGroup>
          )}
        </div>
      )}
      {error && (
        <p className="text-[13px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}

interface AgentProps {
  isTeam?: boolean;
  access: ModelAccess;
  onAccess: (access: ModelAccess) => void;
  selected: string[];
  onSelected: (names: string[]) => void;
  /** The parent level's agents; null = every agent. */
  scope: string[] | null;
  error?: string;
}

/** Which A2A agents a level may call; an agent is granted whole, so a name is the whole choice. */
export function AgentGrants({ isTeam, access, onAccess, selected, onSelected, scope, error }: AgentProps) {
  const perms = useCan();
  const catalog = useA2aAgents(access === "list" && perms.canReadRouting);
  const offered = useMemo(
    () => (scope !== null ? [...scope].sort(byName) : [...(catalog.data ?? []).map((agent) => agent.name)].sort(byName)),
    [scope, catalog.data],
  );
  const exists = (name: string) => !catalog.data || catalog.data.some((agent) => agent.name === name);
  const toggle = (name: string, on: boolean) => onSelected(on ? [...selected, name].sort(byName) : selected.filter((item) => item !== name));
  const notOffered = selected.filter((name) => !offered.includes(name));
  return (
    <fieldset className="flex min-w-0 flex-col gap-2" data-testid="agent-grants">
      <Legend
        title="A2A access"
        hint={
          access === "list"
            ? isTeam
              ? "Only the agents ticked below."
              : "Only the agents ticked below, within what the team allows; nothing ticked denies every agent."
            : isTeam
              ? "Every agent, including ones added later."
              : "Whatever the team allows."
        }
      />
      <AccessChoice legend="A2A access" what="agent" isTeam={isTeam} access={access} onAccess={onAccess} error={error} />
      {access === "list" && (
        <div className="flex max-h-72 flex-col gap-3 overflow-y-auto rounded-md border p-3" data-testid="agent-grant-options">
          {catalog.isPending && perms.canReadRouting && <Skeleton className="h-4 w-full" />}
          {catalog.error && (
            <p className="text-[13px] text-destructive" role="alert">
              {catalog.error.message}
            </p>
          )}
          {scope === null && !perms.canReadRouting && <Empty text="Only an installation admin can list the A2A agents, so this level has no names to narrow." />}
          {!catalog.isPending && offered.length === 0 && notOffered.length === 0 && scope !== null && <Empty text="The parent allows no A2A agent." />}
          {!catalog.isPending && offered.length === 0 && notOffered.length === 0 && scope === null && perms.canReadRouting && <Empty text="No A2A agent exists yet." />}
          {offered.length > 0 && (
            <div className="grid gap-1.5 sm:grid-cols-2">
              {offered.map((name, index) => (
                <div key={name} className="flex items-center gap-2">
                  <Checkbox id={`agent-${index}`} checked={selected.includes(name)} onCheckedChange={(checked) => toggle(name, checked === true)} />
                  <Label htmlFor={`agent-${index}`} className="min-w-0 cursor-pointer font-normal [overflow-wrap:anywhere]">
                    {name}
                  </Label>
                </div>
              ))}
            </div>
          )}
          {notOffered.length > 0 && (
            <NotOfferedGroup label="A2A agents not offered">
              {notOffered.map((name) => (
                <NotOffered
                  key={name}
                  id={`agent-stale-${name}`}
                  name={name}
                  reason={exists(name) ? "(outside the parent's scope)" : "(no longer exists)"}
                  onClear={() => toggle(name, false)}
                />
              ))}
            </NotOfferedGroup>
          )}
        </div>
      )}
      {error && (
        <p className="text-[13px] text-destructive" role="alert">
          {error}
        </p>
      )}
    </fieldset>
  );
}
