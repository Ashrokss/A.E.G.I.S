import { useState } from "react";
import type { FormField, FormSpec } from "../../lib/types";
import { Button, Field, inputClass } from "../../components/ui";
import Markdown from "../../components/Markdown";

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

/** The agent's ask_user_form, as a real form. Answers go back as one message. */
export default function FormCard({
  spec,
  answered,
  onSubmit,
}: {
  spec: FormSpec;
  answered: boolean;
  onSubmit: (text: string) => void;
}) {
  const [values, setValues] = useState<Values>(() => initial(spec.fields));
  const [submitted, setSubmitted] = useState(false);
  const sent = submitted || answered;
  if (answered) {
    return (
      <p className="text-sm text-muted">
        AEGIS asked: <span className="text-fg">{spec.title}</span>
      </p>
    );
  }
  const set = (name: string, value: Values[string]) => setValues((v) => ({ ...v, [name]: value }));

  return (
    <form
      className="grid gap-4 rounded-lg border border-line bg-surface p-4"
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
      <div>
        <p className="font-semibold">{spec.title}</p>
        {spec.description && (
          <div className="mt-1 text-sm text-muted">
            <Markdown>{spec.description}</Markdown>
          </div>
        )}
      </div>
      <fieldset disabled={sent} className="grid gap-4">
        {spec.fields.map((f) => (
          <div key={f.name}>
            {f.type === "checkbox" ? (
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={Boolean(values[f.name])} onChange={(e) => set(f.name, e.target.checked)} className="size-4 accent-[var(--teal)]" />
                {f.label}
              </label>
            ) : f.type === "multiselect" ? (
              <fieldset className="grid gap-1.5">
                <legend className="mb-1.5 text-sm font-medium">{f.label}</legend>
                {f.options?.map((o) => {
                  const list = values[f.name] as string[];
                  return (
                    <label key={o} className="flex items-center gap-2 text-sm">
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
              <Field label={f.label} hint={f.help}>
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
            )}
          </div>
        ))}
      </fieldset>
      <Button type="submit" variant="primary" disabled={sent} className="w-fit">
        {sent ? "Answers sent" : "Send answers"}
      </Button>
    </form>
  );
}
