// A tiny GitHub REST API client: just the calls the MCP server needs.
// It reads Job Radar's files and writes Claude's submissions as new files,
// using a fine-grained token that can only touch the job-radar repository.

export interface Env {
  MCP_SECRET: string;    // the secret key in the connector URL
  GITHUB_TOKEN: string;  // fine-grained token: job-radar only
  GITHUB_REPO: string;   // "owner/job-radar"
  GITHUB_BRANCH?: string;
}

export class GitHubError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const API = "https://api.github.com";

export class GitHub {
  constructor(private env: Env) {}

  private get branch(): string {
    return this.env.GITHUB_BRANCH || "main";
  }

  private async call(method: string, path: string, body?: unknown, accept = "application/vnd.github+json"): Promise<Response> {
    const resp = await fetch(`${API}/repos/${this.env.GITHUB_REPO}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${this.env.GITHUB_TOKEN}`,
        Accept: accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "job-radar-mcp",
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) {
      // Never pass GitHub's raw error back: it can echo request details.
      throw new GitHubError(resp.status, `GitHub answered ${resp.status} for ${method} ${path.split("?")[0]}`);
    }
    return resp;
  }

  /** A file's text from the repo (raw, so files over 1 MB work too). */
  async readText(path: string): Promise<string> {
    const resp = await this.call("GET", `/contents/${path}?ref=${encodeURIComponent(this.branch)}`, undefined,
      "application/vnd.github.raw+json");
    return resp.text();
  }

  async readJson<T>(path: string): Promise<T> {
    return JSON.parse(await this.readText(path)) as T;
  }

  /** Create a new file. Each submission gets its own file, so two never collide. */
  async createFile(path: string, content: string, message: string): Promise<void> {
    await this.call("PUT", `/contents/${path}`, { message, content: toBase64(content), branch: this.branch });
  }

  async addLabel(issue: number, label: string): Promise<void> {
    await this.call("POST", `/issues/${issue}/labels`, { labels: [label] });
  }

  async startWorkflow(file: string): Promise<void> {
    await this.call("POST", `/actions/workflows/${file}/dispatches`, { ref: this.branch });
  }
}

export function toBase64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}
