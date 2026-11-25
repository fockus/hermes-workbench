// JSON-lines bridge over stdio: numbered calls answered by `{id, ok, result | error}` replies,
// fire-and-forget notifications, and every other inbound line handed to `onControl`. A line that is
// not a JSON object (NaN, a torn write) fails every pending call and is reported to `onError`.
export function createBridge(write, onControl, onError) {
  const pending = new Map();
  let nextId = 1;
  const send = (msg) => write(JSON.stringify(msg) + "\n");
  const broken = () => {
    const error = new Error("the host sent a line that is not a JSON object");
    for (const waiter of pending.values()) waiter.reject(error);
    pending.clear();
    onError?.(error.message);
  };
  return {
    notify: send,
    call(op, payload) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject, op });
        send({ op, id, ...payload });
      });
    },
    /** One inbound line. A reply settles its call; anything else is a control message. */
    receive(line) {
      let msg;
      try {
        msg = JSON.parse(line);
      } catch {
        return broken();
      }
      if (msg === null || typeof msg !== "object" || Array.isArray(msg)) return broken();
      const waiter = msg.op === undefined ? pending.get(msg.id) : undefined;
      if (waiter === undefined) {
        if (msg.op !== undefined) onControl(msg);
        return;
      }
      pending.delete(msg.id);
      if (msg.ok === false) waiter.reject(new Error(String(msg.error ?? `the host failed the ${waiter.op} call`)));
      else waiter.resolve(msg.result);
    },
  };
}
