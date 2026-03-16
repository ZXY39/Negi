// @ts-nocheck
import { ChinaChannelsHost } from "./host.js";
import { loadHostConfig } from "./config.js";

function readArg(name: string): string | undefined {
  const idx = process.argv.indexOf(name);
  if (idx < 0) return undefined;
  return process.argv[idx + 1];
}

const configPath = readArg("--config") ?? process.env.G3KU_CONFIG_PATH ?? ".g3ku/config.json";
const cfg = loadHostConfig(configPath);
const host = new ChinaChannelsHost(cfg);

await host.start();

const shutdown = async () => {
  await host.stop();
  process.exit(0);
};

process.on("SIGINT", () => void shutdown());
process.on("SIGTERM", () => void shutdown());
