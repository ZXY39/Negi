// @ts-nocheck
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const REGISTRY_PATH = path.resolve(__dirname, "..", "channel_registry.json");

type RegistryChannel = {
  id: string;
  label?: string;
  description?: string;
  maintenance_status?: string;
  supports_accounts?: boolean;
  probe_strategy?: string;
  secret_fields?: string[];
  legacy_keys?: string[];
  template_json?: Record<string, unknown>;
};

type RegistryPayload = {
  schema_version?: number;
  channels?: RegistryChannel[];
};

let cachedRegistry: RegistryPayload | null = null;

export function loadChannelRegistry(): RegistryPayload {
  if (cachedRegistry) {
    return cachedRegistry;
  }
  const payload = JSON.parse(fs.readFileSync(REGISTRY_PATH, "utf-8")) as RegistryPayload;
  cachedRegistry = payload && typeof payload === "object" ? payload : { channels: [] };
  return cachedRegistry;
}

export function listRegisteredChannels(): RegistryChannel[] {
  const channels = loadChannelRegistry().channels;
  return Array.isArray(channels) ? channels.filter((item) => item && typeof item === "object") : [];
}

