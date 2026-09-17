import { Container, getRandom } from "@cloudflare/containers";
import { env } from "cloudflare:workers";
import { containerEnvVars, containerInstanceCount } from "./container-env";

/**
 * Durable Object that owns one FastAPI container instance.
 * `envVars` are applied on every start from Worker vars + secrets.
 */
export class KwamiApiContainer extends Container<Env> {
  defaultPort = 8080;
  requiredPorts = [8080];
  sleepAfter = "10m";
  enableInternet = true;
  pingEndpoint = "/health";
  envVars = containerEnvVars(env);

  override onStart(): void {
    console.log(
      JSON.stringify({ event: "container_start", class: "KwamiApiContainer" }),
    );
  }

  override onStop(): void {
    console.log(
      JSON.stringify({ event: "container_stop", class: "KwamiApiContainer" }),
    );
  }

  override onError(error: unknown): void {
    console.error(
      JSON.stringify({
        event: "container_error",
        error: error instanceof Error ? error.message : String(error),
      }),
    );
  }
}

function withForwardedHeaders(request: Request): Request {
  const url = new URL(request.url);
  const headers = new Headers(request.headers);
  headers.set("X-Forwarded-Proto", url.protocol.replace(":", "") || "https");
  headers.set("X-Forwarded-Host", url.host);
  const ip = request.headers.get("CF-Connecting-IP");
  if (ip && !headers.has("X-Forwarded-For")) {
    headers.set("X-Forwarded-For", ip);
  }
  return new Request(request, { headers });
}

export default {
  async fetch(request, workerEnv): Promise<Response> {
    try {
      const instances = containerInstanceCount(workerEnv);
      const container = await getRandom(workerEnv.KWAMI_API, instances);
      return await container.fetch(withForwardedHeaders(request));
    } catch (error) {
      console.error(
        JSON.stringify({
          event: "container_proxy_error",
          error: error instanceof Error ? error.message : String(error),
        }),
      );
      return Response.json({ error: "upstream_unavailable" }, { status: 503 });
    }
  },
} satisfies ExportedHandler<Env>;
