
# CBZ GUI UX Revision Patch

## Problems observed

The current GUI layout has several structural UX issues:

- descriptions are rendered on a single line and get clipped
- option labels can overflow the available width
- future longer tool names/options will continue breaking layout
- the output panel competes vertically with the options area
- no command preview exists before launch
- no scrolling region for long option sets
- left sidebar is fixed-width and non-scrollable

---

# Recommended structural changes

## 1. Make the main content area responsive

Use weighted grid columns and rows:

```python
root.grid_columnconfigure(1, weight=1)
root.grid_rowconfigure(0, weight=1)

content.grid_columnconfigure(0, weight=1)
content.grid_rowconfigure(3, weight=1)
```

This prevents clipping when the window is resized.

---

## 2. Wrap descriptions instead of truncating

Current issue:

```python
tk.Label(..., text=description)
```

Replace with:

```python
desc = tk.Label(
    parent,
    text=description,
    wraplength=900,
    justify="left",
    anchor="w",
)
```

Critical settings:

- `wraplength`
- `justify="left"`
- `anchor="w"`

This allows future longer descriptions safely.

---

## 3. Use a scrollable options panel

Instead of packing controls directly into a frame:

```python
options_frame = tk.Frame(content)
```

Use:

```python
canvas = tk.Canvas(content, highlightthickness=0)
scroll = tk.Scrollbar(content, orient="vertical", command=canvas.yview)

scrollable = tk.Frame(canvas)

scrollable.bind(
    "<Configure>",
    lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
)

canvas.create_window((0, 0), window=scrollable, anchor="nw")
canvas.configure(yscrollcommand=scroll.set)
```

Benefits:

- future-proof for many controls
- avoids vertical clipping
- better scaling on small displays

---

## 4. Add a command preview panel

Before launching a tool, show the exact generated command.

Example:

```python
preview_var = tk.StringVar()

preview = tk.Text(
    content,
    height=3,
    bg="#111122",
    fg="#dddddd",
    wrap="word",
)
```

Update dynamically:

```python
preview.delete("1.0", "end")
preview.insert("1.0", " ".join(cmd))
```

Benefits:

- transparency
- easier debugging
- easier copy/paste testing

---

## 5. Use a two-column form layout

Instead of:

```python
label.pack(side="left")
entry.pack(side="left")
```

Use grid:

```python
form.grid_columnconfigure(1, weight=1)

label.grid(row=row, column=0, sticky="w")
entry.grid(row=row, column=1, sticky="ew")
```

Benefits:

- cleaner alignment
- prevents label clipping
- easier responsive scaling

---

## 6. Add sidebar scrolling

Current tool count is already large.

Wrap the sidebar in a scrollable canvas:

```python
sidebar_canvas = tk.Canvas(sidebar)
sidebar_scroll = tk.Scrollbar(sidebar, orient="vertical")
```

This prevents future overflow issues as more tools are added.

---

## 7. Improve spacing consistency

Recommended spacing constants:

```python
PAD_X = 12
PAD_Y = 8
SECTION_GAP = 18
```

Avoid mixed magic numbers.

---

## 8. Separate Output from Controls

Current output area visually crowds the controls.

Recommended structure:

```text
+------------------------------------------+
| Title + Description                      |
+------------------------------------------+
| Scrollable Options Area                  |
+------------------------------------------+
| Command Preview                          |
+------------------------------------------+
| Run / Stop Buttons                       |
+------------------------------------------+
| Output Console                           |
+------------------------------------------+
```

---

## 9. Add minimum window size

```python
root.minsize(1200, 750)
```

Prevents unusable compressed layouts.

---

## 10. Use ttk widgets where possible

Especially:

- Combobox
- Scrollbar
- Notebook
- Treeview

They scale better on Windows DPI settings.

---

# Immediate quick fixes

If you want the fastest possible improvement right now:

### Replace every description label with:

```python
tk.Label(
    parent,
    text=description,
    wraplength=850,
    justify="left",
    anchor="w",
)
```

### Change option rows to grid layout

### Add:

```python
content.grid_columnconfigure(0, weight=1)
```

### Increase minimum window width:

```python
root.minsize(1300, 800)
```

Those four changes alone will solve most clipping problems immediately.

---

# Recommended next evolution

Long-term, this GUI would benefit from:

- tabbed workflow sections
- collapsible advanced options
- persistent saved presets
- recent-path history
- progress bars per task
- live log filtering
- task queueing
- dark/light themes
- threaded task manager panel
- JSON-based dynamic tool registration

The current tool-definition architecture is already close to supporting this cleanly.
