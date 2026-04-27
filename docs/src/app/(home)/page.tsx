import Link from 'next/link';

export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col items-center justify-center px-6 py-20 text-center">
      <div className="mx-auto max-w-3xl">
        <p className="text-fd-muted-foreground mb-4 text-sm tracking-wide uppercase">
          Claude Agent SDK over HTTP+SSE
        </p>
        <h1 className="mb-6 text-5xl font-bold tracking-tight sm:text-6xl">
          Drive Claude agents from the web,
          <br />
          without rewriting the SDK.
        </h1>
        <p className="text-fd-muted-foreground mb-10 text-lg leading-relaxed sm:text-xl">
          A typed JS SDK + Python server library that exposes the Claude Agent SDK
          over a streaming wire protocol. Full streaming, steering, permission
          approvals, and <code className="font-mono text-base">AskUserQuestion</code>{' '}
          handling — out of the box.
        </p>

        <div className="mb-16 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/docs/getting-started"
            className="bg-fd-primary text-fd-primary-foreground hover:bg-fd-primary/90 rounded-lg px-6 py-3 font-medium transition"
          >
            Get started →
          </Link>
          <Link
            href="/docs"
            className="bg-fd-secondary text-fd-secondary-foreground hover:bg-fd-secondary/80 rounded-lg px-6 py-3 font-medium transition"
          >
            Read the docs
          </Link>
          <a
            href="https://github.com/BlitzJB/agent-webkit"
            target="_blank"
            rel="noreferrer"
            className="hover:bg-fd-accent rounded-lg border px-6 py-3 font-medium transition"
          >
            GitHub
          </a>
        </div>

        <div className="grid gap-4 text-left sm:grid-cols-3">
          <Card
            title="@agent-webkit/core"
            href="/docs/guides/frontend-vanilla"
            badge="npm"
            description="Isomorphic JS transport — POST + SSE with auto-reconnect via Last-Event-ID. Works in browser, Node, Deno, Bun."
          />
          <Card
            title="@agent-webkit/react"
            href="/docs/guides/frontend-react"
            badge="npm"
            description="useAgentSession() React hook with delta reconciliation, permission UI states, and AskUserQuestion routing."
          />
          <Card
            title="agent-webkit-server"
            href="/docs/guides/backend-fastapi"
            badge="PyPI"
            description="Python library: session lifecycle, SDK bridge, FastAPI + Postgres adapters bundled. Drop into any ASGI app."
          />
        </div>

        <p className="text-fd-muted-foreground mt-10 text-sm">
          v0.2.0 · <code className="font-mono">protocol_version = "1.0"</code> · MIT
        </p>
      </div>
    </main>
  );
}

function Card({
  title,
  description,
  href,
  badge,
}: {
  title: string;
  description: string;
  href: string;
  badge: string;
}) {
  return (
    <Link
      href={href}
      className="hover:bg-fd-accent group block rounded-xl border p-5 transition"
    >
      <div className="mb-2 flex items-center justify-between">
        <code className="font-mono text-sm font-semibold">{title}</code>
        <span className="bg-fd-secondary text-fd-muted-foreground rounded px-2 py-0.5 font-mono text-[10px] uppercase">
          {badge}
        </span>
      </div>
      <p className="text-fd-muted-foreground text-sm leading-relaxed">
        {description}
      </p>
    </Link>
  );
}
