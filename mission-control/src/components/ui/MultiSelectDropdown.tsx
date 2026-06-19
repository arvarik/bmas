import React, { useState, useRef, useEffect } from "react";
import { ChevronDown, Check } from "lucide-react";

export interface DropdownOption {
  value: string;
  label: React.ReactNode;
  count?: number;
}

export interface MultiSelectDropdownProps {
  label: React.ReactNode;
  icon?: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
  options: DropdownOption[];
  selected: Set<string>;
  onChange: (selected: Set<string>) => void;
  color?: string;
}

export function MultiSelectDropdown({
  label,
  icon: Icon,
  options,
  selected,
  onChange,
  color = "var(--text-secondary)",
}: MultiSelectDropdownProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    if (open) document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [open]);

  const toggleOption = (value: string) => {
    const next = new Set(selected);
    if (next.has(value)) {
      next.delete(value);
    } else {
      next.add(value);
    }
    onChange(next);
  };

  const isActive = selected.size > 0;

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        display: "inline-flex",
        flexShrink: 0,
      }}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          padding: "3px 9px",
          borderRadius: "var(--radius-full)",
          border: `1px solid ${isActive || open ? color : "var(--border-subtle)"}`,
          background: isActive || open ? "var(--surface-hover)" : "transparent",
          color: isActive || open ? "var(--text-secondary)" : "var(--text-tertiary)",
          cursor: "pointer",
          fontSize: "var(--text-xs)",
          transition: "all 150ms ease",
        }}
      >
        {Icon && <Icon size={12} style={{ color: isActive || open ? color : undefined }} />}
        <span>{label}</span>
        {selected.size > 0 && (
          <span
            style={{
              background: color,
              color: "white",
              borderRadius: "var(--radius-full)",
              padding: "0 4px",
              fontSize: "9px",
              fontWeight: "bold",
              lineHeight: 1.2,
            }}
          >
            {selected.size}
          </span>
        )}
        <ChevronDown
          size={12}
          style={{
            transform: open ? "rotate(180deg)" : "rotate(0deg)",
            transition: "transform 150ms ease",
            opacity: 0.7,
            marginLeft: 2,
          }}
        />
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            top: "100%",
            left: 0,
            marginTop: "var(--space-1)",
            background: "var(--surface-overlay)",
            border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-md)",
            minWidth: 180,
            maxHeight: 250,
            overflowY: "auto",
            zIndex: 50,
            boxShadow: "0 8px 16px rgba(0,0,0,0.4)",
            display: "flex",
            flexDirection: "column",
            padding: "var(--space-1) 0",
            animation: "slide-down 150ms ease",
          }}
        >
          {options.length === 0 ? (
            <div style={{ padding: "var(--space-2) var(--space-3)", color: "var(--text-tertiary)", fontSize: "var(--text-xs)" }}>
              No options
            </div>
          ) : (
            options.map((opt) => {
              const isSelected = selected.has(opt.value);
              return (
                <button
                  key={opt.value}
                  type="button"
                  onClick={() => toggleOption(opt.value)}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "var(--space-2)",
                    padding: "var(--space-2) var(--space-3)",
                    background: isSelected ? "var(--surface-active)" : "transparent",
                    border: "none",
                    cursor: "pointer",
                    textAlign: "left",
                    color: isSelected ? "var(--text-primary)" : "var(--text-secondary)",
                    fontSize: "var(--text-xs)",
                    width: "100%",
                    transition: "background 150ms ease",
                  }}
                  onMouseEnter={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = "var(--surface-hover)";
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) (e.currentTarget as HTMLButtonElement).style.background = "transparent";
                  }}
                >
                  <div
                    style={{
                      width: 14,
                      height: 14,
                      borderRadius: 3,
                      border: `1px solid ${isSelected ? color : "var(--border-strong)"}`,
                      background: isSelected ? color : "transparent",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    {isSelected && <Check size={10} color="white" strokeWidth={3} />}
                  </div>
                  <div style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {opt.label}
                  </div>
                  {opt.count !== undefined && (
                    <span style={{ fontSize: "10px", fontFamily: "var(--font-mono)", color: "var(--text-tertiary)" }}>
                      {opt.count}
                    </span>
                  )}
                </button>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
