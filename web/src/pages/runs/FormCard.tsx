import { useState } from "react";
import type { FormField, FormSpec } from "@/lib/types";
import { Button, Field, inputClass } from "@/components/ui";
import Markdown from "@/components/Markdown";

type Values = Record<string, string | string[] | boolean>;

function initial(fields: FormField[]): Values {
  const v: Values = {};
  for (const f of fields) {
    if (f.type === "checkbox") v[f.name] = Boolean(f.default);
    else if (f.type === "multiselect") v[f.name] = Array.isArray(f.default) ? f.default : [];
    else v[f.name] = typeof f.default === "string" ? f.default : f.type === "select" ? (f.options?.[0] ?? "") : "";
  }
  return v;
}

/** The agent's ask_user_form as a real form. Answers go back as one message. */
export default function FormCard({ spec, answered, onSubmit }: { spec: FormSpec; answered: boolean; onSubmit: (t: string) => void }) {
  const [values, setValues] = useState<Values>(() => initial(spec.fields));
  const [submitted, setSubmitted] = useState(false);
  const set = (name: string, value: Values[string]) => setValues((v) => ({ ...v, [name]: value }));

  if (answered) {
    return <p className="text-sm text-muted">AEGIS asked: {spec.title}</p>;
  }

  return (
    <form
      className="grid gap-5 rounded-2xl border border-line bg-surface p-5"
      onSubmit={(e) => {
        e.preventDefault();
        const lines = spec.fields.map((f) => {
          const v = values[f.name];
          const shown = Array.isArray(v) ? v.join(", ") || "none" : typeof v === "boolean" ? (v ? "yes" : "no") : v || "not specified";
          return `- **${f.label}**: ${shown}`;
        });
        onSubmit(lines.join("\n"));
        setSubmitted(true);
      }}
    >
      <div className="grid gap-1">
        <p className="font-display font-semibold">{spec.title}</p>
        {spec.description && (
          <div className="text-sm text-muted">
            <Markdown>{spec.description}</Markdown>
          </div>
        )}
      </div>
      <fieldset disabled={submitted} className="grid gap-4">
        {spec.fields.map((f) =>
          f.type === "checkbox" ? (
            <label key={f.name} className="flex items-center gap-3 text-sm">
              <input
                type="checkbox"
                checked={Boolean(values[f.name])}
                onChange={(e) => set(f.name, e.target.checked)}
                className="size-4 accent-[var(--teal)]"
              />
              {f.label}
            </label>
          ) : f.type === "multiselect" ? (
            <fieldset key={f.name} className="grid gap-2">
              <legend className="mb-1 text-sm font-medium">{f.label}</legend>
              {f.options?.map((o) => {
                const list = values[f.name] as string[];
                return (
                  <label key={o} className="flex items-center gap-3 text-sm">
                    <input
                      type="checkbox"
                      className="size-4 accent-[var(--teal)]"
                      checked={list.includes(o)}
                      onChange={(e) => set(f.name, e.target.checked ? [...list, o] : list.filter((x) => x !== o))}
                    />
                    {o}
                  </label>
                );
              })}
            </fieldset>
          ) : (
            <Field key={f.name} label={f.label} hint={f.help}>
              {f.type === "select" ? (
                <select value={values[f.name] as string} onChange={(e) => set(f.name, e.target.value)} className={inputClass}>
                  {f.options?.map((o) => (
                    <option key={o}>{o}</option>
                  ))}
                </select>
              ) : (
                <input value={values[f.name] as string} onChange={(e) => set(f.name, e.target.value)} className={inputClass} />
              )}
            </Field>
          ),
        )}
      </fieldset>
      <Button type="submit" variant="primary" disabled={submitted} className="w-full sm:w-fit">
        {submitted ? "Answers sent" : "Send answers"}
      </Button>
    </form>
  );
}
