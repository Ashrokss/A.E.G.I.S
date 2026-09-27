/** The AEGIS mark: a shield holding Terraform's "create" glyph. */
export default function Mark({ className = "size-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <path
        d="M16 2 4 7v9c0 7 5 12 12 14 7-2 12-7 12-14V7z"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinejoin="round"
      />
      <path d="M16 10v12M10 16h12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}
