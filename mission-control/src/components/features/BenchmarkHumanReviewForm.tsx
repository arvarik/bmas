"use client";

import { useRef, useState, type FormEvent } from "react";
import { ActionButton } from "@/components/ui/ActionButton";
import type { BenchmarkHumanReview } from "@/lib/benchmarks";
import { Select } from "@/components/ui/Select";

interface BenchmarkHumanReviewFormProps {
  attemptId: string;
  existing?: BenchmarkHumanReview;
  onSaved: () => Promise<void>;
}

export function BenchmarkHumanReviewForm({
  attemptId,
  existing,
  onSaved,
}: BenchmarkHumanReviewFormProps) {
  const [score, setScore] = useState("1");
  const [passed, setPassed] = useState(true);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const idempotencyKey = useRef<string | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      idempotencyKey.current ??= crypto.randomUUID();
      const response = await fetch(
        `/api/benchmarks/attempts/${encodeURIComponent(attemptId)}/reviews`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Idempotency-Key": idempotencyKey.current,
          },
          body: JSON.stringify({ score: Number(score), passed, note }),
        },
      );
      const data = await response.json() as { detail?: string };
      if (!response.ok) throw new Error(data.detail ?? "The review could not be saved");
      await onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The review could not be saved");
    } finally {
      setPending(false);
    }
  };

  if (existing) {
    return (
      <div className="benchmark-human-review benchmark-human-review--saved">
        <strong>Human review</strong>
        <span>{existing.passed ? "Pass" : "Fail"} · {(existing.score * 100).toFixed(0)}%</span>
        {existing.note ? <p>{existing.note}</p> : null}
        <small>{existing.reviewer_id} · {new Date(existing.created_at).toLocaleString()}</small>
      </div>
    );
  }

  return (
    <form className="benchmark-human-review" onSubmit={submit}>
      <strong>Human review</strong>
      <label>Outcome<Select value={passed ? "pass" : "fail"} onChange={(event) => setPassed(event.target.value === "pass")}><option value="pass">Pass</option><option value="fail">Fail</option></Select></label>
      <label>Score from 0 to 1<input required type="number" min="0" max="1" step="0.01" value={score} onChange={(event) => setScore(event.target.value)} /></label>
      <label>Review note<textarea rows={2} value={note} onChange={(event) => setNote(event.target.value)} /></label>
      {error ? <p role="alert">{error}</p> : null}
      <ActionButton type="submit" loading={pending}>Save immutable review</ActionButton>
    </form>
  );
}
