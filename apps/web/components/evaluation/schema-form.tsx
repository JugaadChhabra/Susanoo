"use client";

import { useId } from "react";

import { Plus, Trash2 } from "lucide-react";
import type { VersionRecord } from "@/types/platform-generated";
import rawSchema from "@/types/platform-schema.json";

type Schema = {
  type?: string;
  title?: string;
  $ref?: string;
  default?: unknown;
  const?: unknown;
  enum?: unknown[];
  properties?: Record<string, Schema>;
  required?: string[];
  items?: Schema;
  anyOf?: Schema[];
  minimum?: number;
  maximum?: number;
  minLength?: number;
  maxLength?: number;
};
const definitions = rawSchema.$defs as Record<string, Schema>;
export const schemaDefinitions = definitions;
const label = (name: string) =>
  name.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());

function resolve(schema: Schema): Schema {
  if (schema.$ref) return resolve(definitions[schema.$ref.split("/").at(-1)!]);
  if (schema.anyOf)
    return resolve(schema.anyOf.find((item) => item.type !== "null") ?? {});
  return schema;
}

export function initialValue(source: Schema): unknown {
  if ("default" in source) return source.default;
  const schema = resolve(source);
  if ("default" in schema) return schema.default;
  if ("const" in schema) return schema.const;
  if (schema.enum) return schema.enum[0];
  if (schema.type === "object")
    return Object.fromEntries(
      Object.entries(schema.properties ?? {}).map(([key, sub]) => [
        key,
        initialValue(sub),
      ]),
    );
  if (schema.type === "array") return [];
  if (schema.type === "boolean") return false;
  if (schema.type === "integer" || schema.type === "number")
    return schema.minimum ?? 0;
  if (source.anyOf?.some((item) => item.type === "null")) return null;
  return "";
}

const references: Record<string, string> = {
  requirement_version_id: "requirement",
  grader_version_id: "grader",
  fixture_version_id: "fixture",
  check_version_ids: "check",
  case_version_ids: "case",
  environment_version_id: "environment",
  simulator_version_id: "environment",
  reference_version_id: "environment",
  fidelity_version_id: "fidelity",
  positive_control_ids: "case",
  negative_control_ids: "case",
  heldout_control_ids: "case",
  suite_version_id: "suite",
  connection_version_id: "connection",
};

export function SchemaForm({
  name,
  schema: source,
  value,
  onChange,
  versions = [],
  root = false,
}: {
  name: string;
  schema: Schema;
  value: unknown;
  onChange: (value: unknown) => void;
  versions?: VersionRecord[];
  root?: boolean;
}) {
  const id = useId();
  const schema = resolve(source);
  if ("const" in schema) return null;
  const text = label(name);
  const nullableObject =
    source.anyOf?.some((item) => item.type === "null") &&
    schema.type === "object";
  if (nullableObject && value == null)
    return (
      <label className="ev-check">
        <input
          type="checkbox"
          checked={false}
          onChange={() => onChange(initialValue(schema))}
        />
        {text} Enabled
      </label>
    );
  const required =
    !source.anyOf?.some((item) => item.type === "null") &&
    !("default" in source);
  if (schema.type === "object" && schema.properties) {
    const object = (value ?? {}) as Record<string, unknown>;
    const children = Object.entries(schema.properties).map(([key, sub]) => (
      <SchemaForm
        key={key}
        name={key}
        schema={sub}
        value={object[key]}
        versions={versions}
        onChange={(next) => onChange({ ...object, [key]: next })}
      />
    ));
    return root ? (
      <div className="ev-form-grid">{children}</div>
    ) : (
      <fieldset className="ev-fieldset">
        <legend>{text}</legend>
        {nullableObject && (
          <label className="ev-check">
            <input type="checkbox" checked onChange={() => onChange(null)} />
            Enabled
          </label>
        )}
        <div className="ev-form-grid">{children}</div>
      </fieldset>
    );
  }
  if (schema.type === "boolean")
    return (
      <label className="ev-check">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        {text}
      </label>
    );
  if (schema.enum)
    return (
      <label>
        {text}
        <select
          id={id}
          value={String(value ?? schema.enum[0])}
          onChange={(e) => onChange(e.target.value)}
        >
          {schema.enum.map((item) => (
            <option key={String(item)} value={String(item)}>
              {String(item).replaceAll("_", " ")}
            </option>
          ))}
        </select>
      </label>
    );
  if (schema.type === "array") {
    const list = Array.isArray(value) ? value : [];
    const item = resolve(schema.items ?? {});
    if (references[name])
      return (
        <fieldset className="ev-fieldset">
          <legend>{text}</legend>
          <div className="ev-option-list">
            {versions
              .filter((v) => v.kind === references[name])
              .map((v) => (
                <label className="ev-check" key={v.id}>
                  <input
                    type="checkbox"
                    checked={list.includes(v.id)}
                    onChange={(e) =>
                      onChange(
                        e.target.checked
                          ? [...list, v.id]
                          : list.filter((id) => id !== v.id),
                      )
                    }
                  />
                  {v.title} <small>r{v.revision}</small>
                </label>
              ))}
            {!versions.some((v) => v.kind === references[name]) && (
              <span className="ev-muted">
                No {references[name]} revisions available.
              </span>
            )}
          </div>
        </fieldset>
      );
    if (item.type === "object")
      return (
        <fieldset className="ev-fieldset">
          <legend>{text}</legend>
          {list.map((entry, index) => (
            <div className="ev-array-row" key={index}>
              <SchemaForm
                root
                name={`${name}-${index}`}
                schema={schema.items ?? {}}
                value={entry}
                versions={versions}
                onChange={(next) =>
                  onChange(list.map((old, i) => (i === index ? next : old)))
                }
              />
              <button
                type="button"
                className="ev-icon"
                title="Remove item"
                aria-label={`Remove ${text} ${index + 1}`}
                onClick={() => onChange(list.filter((_, i) => i !== index))}
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))}
          <button
            type="button"
            className="ev-secondary"
            onClick={() =>
              onChange([...list, initialValue(schema.items ?? {})])
            }
          >
            <Plus size={15} />
            Add Item
          </button>
        </fieldset>
      );
    return (
      <label>
        {text}
        <textarea
          id={id}
          rows={3}
          value={list.join("\n")}
          onChange={(e) =>
            onChange(
              e.target.value
                .split("\n")
                .filter(Boolean)
                .map((v) => (item.type === "integer" ? Number(v) : v)),
            )
          }
        />
      </label>
    );
  }
  if (references[name])
    return (
      <label>
        {text}
        <select
          id={id}
          required={required}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value || null)}
        >
          <option value="">Select revision</option>
          {versions
            .filter((v) => v.kind === references[name])
            .map((v) => (
              <option key={v.id} value={v.id}>
                {v.title} · r{v.revision}
              </option>
            ))}
        </select>
      </label>
    );
  if (schema.type === "object" || !schema.type)
    return (
      <label>
        {text}
        <textarea
          id={id}
          rows={3}
          value={
            typeof value === "string"
              ? value
              : JSON.stringify(
                  value ?? (schema.type === "object" ? {} : null),
                  null,
                  2,
                )
          }
          onChange={(e) => {
            try {
              onChange(JSON.parse(e.target.value));
            } catch {
              onChange(e.target.value);
            }
          }}
        />
      </label>
    );
  if (schema.type === "integer" || schema.type === "number")
    return (
      <label>
        {text}
        <input
          id={id}
          type="number"
          required={required}
          min={schema.minimum}
          max={schema.maximum}
          value={Number(value ?? schema.minimum ?? 0)}
          onChange={(e) => onChange(Number(e.target.value))}
        />
      </label>
    );
  const long = ["expected", "source_clause", "rubric", "message"].includes(
    name,
  );
  return (
    <label>
      {text}
      {long ? (
        <textarea
          id={id}
          rows={3}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <input
          id={id}
          required={required}
          minLength={schema.minLength}
          maxLength={schema.maxLength}
          value={String(value ?? "")}
          onChange={(e) => onChange(e.target.value || (required ? "" : null))}
        />
      )}
    </label>
  );
}
