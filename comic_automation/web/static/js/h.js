// The only way the console builds DOM (design §11). Text always goes in through
// textContent and attributes through setAttribute, so nothing the server or a
// library file name contains can become markup. Never use innerHTML here or
// anywhere else; tests/web/test_web_static_check.py enforces that.
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key.startsWith("on")) throw new Error("h(): attach listeners with addEventListener, not attributes");
    node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
