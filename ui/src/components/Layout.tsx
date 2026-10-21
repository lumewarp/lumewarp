import { useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { clearToken } from "../auth";
import { useCan, useMe } from "../hooks";
import { useTheme } from "../theme";
import { Badge } from "./Bits";

const NAV = [
  { to: "/", label: "Overview" },
  { to: "/orgs", label: "Organizations" },
  { to: "/teams", label: "Teams" },
  { to: "/projects", label: "Projects" },
  { to: "/keys", label: "API keys" },
  { to: "/routes", label: "Models / Routes" },
  { to: "/deployments", label: "Deployments" },
  { to: "/upstreams", label: "Upstreams" },
  { to: "/usage", label: "Usage" },
  { to: "/audit", label: "Audit log" },
  { to: "/access", label: "Users & access", superOnly: true },
  { to: "/gateways", label: "Gateways", superOnly: true },
  { to: "/config", label: "Config versions", superOnly: true },
  { to: "/settings", label: "Settings" },
] as const;

const ROLE_RANK = ["owner", "admin", "member", "viewer"];
function roleLabel(me: { is_superuser: boolean; grants: { role: string }[] }) {
  if (me.is_superuser) return "superuser";
  return [...me.grants].sort((a, b) => ROLE_RANK.indexOf(a.role) - ROLE_RANK.indexOf(b.role))[0]?.role ?? "no access";
}

const THEME_LABEL = { system: "Auto", light: "Light", dark: "Dark" } as const;

export function Layout() {
  const me = useMe();
  const perms = useCan();
  const { theme, cycle } = useTheme();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [navOpen, setNavOpen] = useState(false);

  const logout = () => {
    clearToken();
    qc.clear();
    void navigate({ to: "/login" });
  };

  return (
    <div className="shell">
      <a className="skip" href="#main">
        Skip to content
      </a>
      <aside className={`sidebar ${navOpen ? "open" : ""}`}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true" />
          Lumewarp
        </div>
        <nav aria-label="Main">
          {NAV.filter((n) => !("superOnly" in n) || perms.isSuper).map((n) => (
            <Link key={n.to} to={n.to} className="nav-link" activeProps={{ className: "nav-link active", "aria-current": "page" }} activeOptions={{ exact: n.to === "/" }} onClick={() => setNavOpen(false)}>
              {n.label}
            </Link>
          ))}
        </nav>
      </aside>
      <div className="main-col">
        <header className="topbar">
          <button className="btn btn-sm menu-btn" aria-label="Toggle navigation" aria-expanded={navOpen} onClick={() => setNavOpen((o) => !o)}>
            ☰
          </button>
          <span className="spacer" />
          <button className="btn btn-sm" onClick={cycle} title="Cycle theme">
            Theme: {THEME_LABEL[theme]}
          </button>
          {me.data && (
            <span className="principal">
              <span>{me.data.display_name}</span>
              <Badge tone="info">{roleLabel(me.data)}</Badge>
            </span>
          )}
          <button className="btn btn-sm" onClick={logout}>
            Sign out
          </button>
        </header>
        <main id="main" className="content" tabIndex={-1}>
          <Outlet />
        </main>
      </div>
    </div>
  );
}
