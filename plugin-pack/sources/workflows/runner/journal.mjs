// The run's journal file (REQ-013). A run writes to journal.jsonl.part and moves it over journal.jsonl
// at open when it replays another file or nothing; on a replay of journal.jsonl itself, at its first
// entry the replayed journal does not already hold, or when it completes. A resume that stops during
// its replay of journal.jsonl so leaves the journal it replayed intact.
import { appendFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const replays = (entry, source) => source !== undefined && entry.index === source.index
  && entry.key === source.key && entry.ok === source.ok && (entry.text ?? "") === (source.text ?? "");

/**
 * Opens the journal at `path` for a run replaying `entries` read from `source`; throws when the file
 * cannot be created. `append` throws when an entry cannot be written.
 */
export function openJournal(path, entries, source = path) {
  const part = `${path}.part`;
  rmSync(part, { force: true });
  writeFileSync(part, "", { mode: 0o600 });
  let current = part;
  const promote = () => {
    renameSync(part, path);
    current = path;
  };
  // Another file stays intact whatever this run does, so the replayed prefix is kept here at once.
  if (entries.length === 0 || resolve(source) !== resolve(path)) promote();
  return {
    append(entry) {
      if (current === part && !replays(entry, entries[entry.index])) {
        try {
          promote();
        } catch {
          // Kept in the part file: a journal that cannot be written costs a future resume, nothing more.
        }
      }
      appendFileSync(current, `${JSON.stringify(entry)}\n`, "utf-8");
    },
    /** Settles the part file of a run that never got past its replay. */
    close(status) {
      if (current !== part) return;
      if (status === "completed") promote();
      else rmSync(part, { force: true });
    },
  };
}
