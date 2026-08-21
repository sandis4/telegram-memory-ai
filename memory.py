"""memory.py — менеджер памяти ИИ-бота (pygame): загрузка, создание, редактирование и удаление md/txt файлов из папки memory."""
from pathlib import Path

import pygame

BASE_DIR = Path(__file__).parent
MEMORY_DIR = BASE_DIR / "memory"
EXTS = (".md", ".txt")

W, H = 960, 640
FPS = 60

BG = (24, 26, 32)
PANEL = (32, 35, 44)
ACCENT = (86, 156, 214)
ACCENT_DIM = (56, 106, 160)
GREEN = (88, 170, 110)
TEXT = (230, 230, 235)
MUTED = (140, 145, 155)
DANGER = (200, 80, 80)
HOVER = (44, 48, 60)
SEL = (52, 80, 120)
CURSOR_COLOR = (255, 210, 90)

MODE_LIST, MODE_EDIT, MODE_NEWNAME = 0, 1, 2


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


class App:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode((W, H))
        pygame.display.set_caption("Память бота")
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("arial", 18)
        self.font_small = pygame.font.SysFont("arial", 14)
        self.font_title = pygame.font.SysFont("arial", 26, bold=True)
        self.font_mono = pygame.font.SysFont("consolas", 18)

        self.mode = MODE_LIST
        self.files = []
        self.selected = None
        self.confirm_delete = False
        self.list_scroll = 0
        self.status = "Номера [file-N] — для тегов в боте. «Загрузить» — импорт, двойной клик — правка"

        self.edit_name = None
        self.text = ""
        self.cursor = 0
        self.scroll_line = 0
        self.dirty = False
        self.wrapped = []

        self.new_name = ""
        self.buttons = {}
        self.rows = []

        self.char_w = max(1, self.font_mono.size("M")[0])
        self.line_h = self.font_mono.get_height() + 5
        self.max_cols = max(10, (W - 48) // self.char_w)
        self.edit_top = 70

        self.refresh()

    # ---------------- данные ----------------

    def refresh(self):
        MEMORY_DIR.mkdir(exist_ok=True)
        self.files = sorted(
            (p for p in MEMORY_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in EXTS),
            key=lambda p: p.name.lower(),
        )
        self.confirm_delete = False
        names = [f.name for f in self.files]
        if self.selected and self.selected not in names:
            self.selected = None

    def total_chars(self) -> int:
        return sum(len(read_text(p)) for p in self.files)

    # ---------------- редактор ----------------

    def rewrap(self):
        self.wrapped = []
        pos = 0
        for raw in self.text.split("\n"):
            start = pos
            i = 0
            while True:
                chunk = raw[i:i + self.max_cols]
                self.wrapped.append((start + i, start + i + len(chunk), chunk))
                i += len(chunk)
                if i >= len(raw):
                    break
            pos += len(raw) + 1

    def open_editor(self, name: str):
        try:
            self.text = read_text(MEMORY_DIR / name)
        except OSError as e:
            self.status = f"Не удалось открыть файл: {e}"
            return
        self.edit_name = name
        self.cursor = len(self.text)
        self.scroll_line = 0
        self.dirty = False
        self.rewrap()
        self.mode = MODE_EDIT

    def save_editor(self):
        try:
            (MEMORY_DIR / self.edit_name).write_text(self.text, encoding="utf-8")
        except OSError as e:
            self.status = f"Ошибка сохранения: {e}"
            return
        self.dirty = False
        self.status = f"Сохранено: {self.edit_name}"
        self.refresh()

    def cursor_line(self) -> int:
        best = 0
        for idx, (s, e, _) in enumerate(self.wrapped):
            if s <= self.cursor < e:
                return idx
            if self.cursor >= e:
                best = idx
        return best

    def visible_lines(self) -> int:
        return max(1, (H - self.edit_top - 60) // self.line_h)

    def ensure_visible(self):
        idx = self.cursor_line()
        vis = self.visible_lines()
        if idx < self.scroll_line:
            self.scroll_line = idx
        elif idx >= self.scroll_line + vis:
            self.scroll_line = idx - vis + 1

    def line_bounds(self):
        ls = self.text.rfind("\n", 0, self.cursor) + 1
        le = self.text.find("\n", self.cursor)
        return ls, (len(self.text) if le == -1 else le)

    def insert(self, ch: str):
        self.text = self.text[:self.cursor] + ch + self.text[self.cursor:]
        self.cursor += len(ch)
        self.after_edit()

    def backspace(self):
        if self.cursor > 0:
            self.text = self.text[:self.cursor - 1] + self.text[self.cursor:]
            self.cursor -= 1
            self.after_edit()

    def delete(self):
        if self.cursor < len(self.text):
            self.text = self.text[:self.cursor] + self.text[self.cursor + 1:]
            self.after_edit()

    def after_edit(self):
        self.dirty = True
        self.rewrap()
        self.ensure_visible()

    def move(self, dx=0, dy=0, home=False, end=False, page=0):
        if home:
            self.cursor = self.line_bounds()[0]
        elif end:
            self.cursor = self.line_bounds()[1]
        elif dy or page:
            idx = self.cursor_line()
            offset = page * self.visible_lines() if page else dy
            tgt = max(0, min(len(self.wrapped) - 1, idx + offset))
            s, e, _ = self.wrapped[tgt]
            col = self.cursor - self.wrapped[idx][0]
            self.cursor = min(s + col, e)
        elif dx:
            self.cursor = max(0, min(len(self.text), self.cursor + dx))
        self.ensure_visible()

    def click_editor(self, pos):
        x, y = pos
        idx = self.scroll_line + int((y - self.edit_top) // self.line_h)
        idx = max(0, min(idx, len(self.wrapped) - 1))
        s, e, disp = self.wrapped[idx]
        col = round((x - 24) / self.char_w)
        self.cursor = s + max(0, min(col, len(disp)))

    # ---------------- действия списка ----------------

    def load_file_dialog(self):
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askopenfilename(
            parent=root,
            title="Выберите md или txt файл",
            filetypes=[("Документы", "*.md *.txt"), ("Все файлы", "*.*")],
        )
        root.destroy()
        if not path:
            return
        src = Path(path)
        dst = MEMORY_DIR / src.name
        n = 1
        while dst.exists():
            dst = MEMORY_DIR / f"{src.stem}_{n}{src.suffix.lower()}"
            n += 1
        try:
            dst.write_bytes(src.read_bytes())
        except OSError as e:
            self.status = f"Ошибка копирования: {e}"
            return
        self.status = f"Загружено: {dst.name}"
        self.selected = dst.name
        self.refresh()

    def create_file(self, name: str):
        name = name.strip()
        if not name:
            return
        if not name.lower().endswith(EXTS):
            name += ".md"
        path = MEMORY_DIR / name
        if path.exists():
            self.status = "Такой файл уже есть"
            return
        path.write_text("", encoding="utf-8")
        self.refresh()
        self.selected = name
        self.open_editor(name)

    def delete_selected(self):
        if not self.selected:
            self.status = "Сначала выбери файл в списке"
            return
        if not self.confirm_delete:
            self.confirm_delete = True
            self.status = f"Удалить «{self.selected}»? Нажми «Удалить» ещё раз"
            return
        try:
            (MEMORY_DIR / self.selected).unlink()
        except OSError as e:
            self.status = f"Ошибка удаления: {e}"
            return
        self.status = f"Удалено: {self.selected}"
        self.selected = None
        self.refresh()

    # ---------------- отрисовка ----------------

    def button(self, key, label, x, y, w, h, color=ACCENT):
        rect = pygame.Rect(x, y, w, h)
        hovered = rect.collidepoint(pygame.mouse.get_pos())
        fill = tuple(min(255, c + 25) for c in color) if hovered else color
        pygame.draw.rect(self.screen, fill, rect, border_radius=8)
        label_surf = self.font.render(label, True, (255, 255, 255))
        self.screen.blit(label_surf, label_surf.get_rect(center=rect.center))
        self.buttons[key] = rect
        return rect

    def draw_list(self):
        self.buttons.clear()
        self.rows.clear()
        self.screen.fill(BG)
        pygame.draw.rect(self.screen, PANEL, (0, 0, W, 56))

        title = self.font_title.render("Память бота", True, TEXT)
        self.screen.blit(title, (20, 12))
        sel_tag = ""
        if self.selected:
            for i, p in enumerate(self.files):
                if p.name == self.selected:
                    sel_tag = f"   |   Выбран: [file-{i + 1}]"
                    break
        info = self.font_small.render(
            f"Файлов: {len(self.files)}   Символов: {self.total_chars()}{sel_tag}",
            True, MUTED,
        )
        self.screen.blit(info, (220, 22))

        top, row_h = 76, 38
        vis = (H - top - 130) // row_h
        self.list_scroll = max(0, min(self.list_scroll, max(0, len(self.files) - vis)))
        mouse = pygame.mouse.get_pos()
        for i, path in enumerate(self.files):
            slot = i - self.list_scroll
            if slot < 0 or slot >= vis:
                continue
            rect = pygame.Rect(20, top + slot * row_h, W - 40, row_h - 6)
            color = SEL if path.name == self.selected else (
                HOVER if rect.collidepoint(mouse) else PANEL
            )
            pygame.draw.rect(self.screen, color, rect, border_radius=6)
            tag_surf = self.font_mono.render(f"[file-{i + 1}]", True, ACCENT)
            self.screen.blit(tag_surf, (rect.x + 12, rect.y + 5))
            name_surf = self.font.render(path.name, True, TEXT)
            self.screen.blit(name_surf, (rect.x + 12 + tag_surf.get_width() + 10, rect.y + 5))
            size_surf = self.font_small.render(
                f"{path.stat().st_size / 1024:.1f} КБ", True, MUTED)
            self.screen.blit(size_surf, size_surf.get_rect(
                midright=(rect.right - 12, rect.centery)))
            self.rows.append((rect, path.name))

        if not self.files:
            hint = self.font.render("Папка memory пуста — нажми «Загрузить» или «Создать»",
                                    True, MUTED)
            self.screen.blit(hint, hint.get_rect(center=(W // 2, top + 60)))

        by, bw, gap = H - 52, 150, 14
        x = 20
        self.button("load", "Загрузить", x, by, bw, 36); x += bw + gap
        self.button("new", "Создать", x, by, bw, 36); x += bw + gap
        self.button("edit", "Редактировать", x, by, bw + 10, 36); x += bw + 10 + gap
        del_color = DANGER if self.confirm_delete else ACCENT
        del_label = "Точно удалить?" if self.confirm_delete else "Удалить"
        self.button("del", del_label, x, by, bw, 36, del_color); x += bw + gap
        self.button("refresh", "Обновить", x, by, bw, 36)

        status = self.font_small.render(self.status, True, MUTED)
        self.screen.blit(status, (20, H - 74))

    def draw_editor(self):
        self.buttons.clear()
        self.screen.fill(BG)
        pygame.draw.rect(self.screen, PANEL, (0, 0, W, 56))
        mark = "*" if self.dirty else ""
        title = self.font_title.render(f"{self.edit_name}{mark}", True,
                                       CURSOR_COLOR if self.dirty else TEXT)
        self.screen.blit(title, (20, 12))
        hint = self.font_small.render("Ctrl+S — сохранить   Esc — назад", True, MUTED)
        self.screen.blit(hint, hint.get_rect(right=(W - 20), centery=28))

        area = pygame.Rect(0, self.edit_top, W, H - self.edit_top - 50)
        vis = self.visible_lines()
        max_scroll = max(0, len(self.wrapped) - vis)
        self.scroll_line = max(0, min(self.scroll_line, max_scroll))

        cursor_drawn = False
        blink = pygame.time.get_ticks() % 1000 < 500
        for i in range(self.scroll_line, min(len(self.wrapped), self.scroll_line + vis)):
            s, e, disp = self.wrapped[i]
            y = self.edit_top + (i - self.scroll_line) * self.line_h
            surf = self.font_mono.render(disp, True, TEXT)
            self.screen.blit(surf, (24, y))
            if blink and not cursor_drawn and s <= self.cursor <= e:
                cx = 24 + (self.cursor - s) * self.char_w
                pygame.draw.rect(self.screen, CURSOR_COLOR, (cx, y, 2, self.line_h - 4))
                cursor_drawn = True

        pygame.draw.line(self.screen, PANEL, (0, area.bottom), (W, area.bottom), 2)

        by = H - 42
        self.button("save", "Сохранить (Ctrl+S)", 20, by, 210, 34, GREEN)
        self.button("back", "Назад (Esc)", W - 130, by, 110, 34, ACCENT_DIM)
        status = self.font_small.render(self.status, True, MUTED)
        self.screen.blit(status, (250, by + 9))

    def draw_newname(self):
        self.buttons.clear()
        self.screen.fill(BG)
        panel = pygame.Rect(W // 2 - 260, H // 2 - 90, 520, 180)
        pygame.draw.rect(self.screen, PANEL, panel, border_radius=10)
        t = self.font.render("Имя нового файла (Enter — создать, Esc — отмена):", True, TEXT)
        self.screen.blit(t, (panel.x + 20, panel.y + 18))
        box = pygame.Rect(panel.x + 20, panel.y + 55, panel.w - 40, 40)
        pygame.draw.rect(self.screen, BG, box, border_radius=6)
        pygame.draw.rect(self.screen, ACCENT, box, 2, border_radius=6)
        self.screen.blit(self.font_mono.render(self.new_name, True, TEXT), (box.x + 10, box.y + 8))
        if pygame.time.get_ticks() % 1000 < 500:
            cx = box.x + 10 + len(self.new_name) * self.char_w
            pygame.draw.rect(self.screen, CURSOR_COLOR, (cx, box.y + 8, 2, 24))
        hint = self.font_small.render("Без расширения добавится .md", True, MUTED)
        self.screen.blit(hint, (panel.x + 20, panel.y + 115))

    # ---------------- события ----------------

    def handle_keydown_list(self, event):
        if event.key in (pygame.K_UP, pygame.K_DOWN):
            if self.files:
                idx = [f.name for f in self.files].index(self.selected) \
                    if self.selected in [f.name for f in self.files] else -1
                idx = max(0, min(len(self.files) - 1, idx + (1 if event.key == pygame.K_DOWN else -1)))
                self.selected = self.files[idx].name
        elif event.key == pygame.K_RETURN:
            if self.selected:
                self.open_editor(self.selected)
        elif event.key == pygame.K_DELETE:
            self.delete_selected()

    def handle_keydown_edit(self, event):
        ctrl = event.mod & pygame.KMOD_CTRL
        if ctrl and event.key == pygame.K_s:
            self.save_editor()
        elif event.key == pygame.K_ESCAPE:
            self.mode = MODE_LIST
            self.refresh()
        elif event.key == pygame.K_BACKSPACE:
            self.backspace()
        elif event.key == pygame.K_DELETE:
            self.delete()
        elif event.key == pygame.K_RETURN:
            self.insert("\n")
        elif event.key == pygame.K_LEFT:
            self.move(dx=-1)
        elif event.key == pygame.K_RIGHT:
            self.move(dx=1)
        elif event.key == pygame.K_UP:
            self.move(dy=-1)
        elif event.key == pygame.K_DOWN:
            self.move(dy=1)
        elif event.key == pygame.K_HOME:
            self.move(home=True)
        elif event.key == pygame.K_END:
            self.move(end=True)
        elif event.key == pygame.K_PAGEUP:
            self.move(page=-1)
        elif event.key == pygame.K_PAGEDOWN:
            self.move(page=1)

    def run(self):
        last_click = 0
        running = True
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

                elif event.type == pygame.MOUSEWHEEL:
                    if self.mode == MODE_LIST:
                        self.list_scroll -= event.y
                    elif self.mode == MODE_EDIT:
                        self.scroll_line -= event.y * 3

                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    if self.mode == MODE_LIST:
                        clicked_btn = None
                        for key, rect in self.buttons.items():
                            if rect.collidepoint(event.pos):
                                clicked_btn = key
                                break
                        if clicked_btn == "load":
                            self.load_file_dialog()
                        elif clicked_btn == "new":
                            self.new_name = ""
                            self.mode = MODE_NEWNAME
                        elif clicked_btn == "edit":
                            if self.selected:
                                self.open_editor(self.selected)
                            else:
                                self.status = "Сначала выбери файл в списке"
                        elif clicked_btn == "del":
                            self.delete_selected()
                        elif clicked_btn == "refresh":
                            self.refresh()
                            self.status = "Список обновлён"
                        elif not clicked_btn:
                            hit = next((name for rect, name in self.rows
                                        if rect.collidepoint(event.pos)), None)
                            if hit:
                                now = pygame.time.get_ticks()
                                dbl = now - last_click < 350 and self.selected == hit
                                last_click = now
                                self.selected = hit
                                self.confirm_delete = False
                                if dbl:
                                    self.open_editor(hit)
                    elif self.mode == MODE_EDIT:
                        if self.buttons.get("save") and self.buttons["save"].collidepoint(event.pos):
                            self.save_editor()
                        elif self.buttons.get("back") and self.buttons["back"].collidepoint(event.pos):
                            self.mode = MODE_LIST
                            self.refresh()
                        elif self.edit_top <= event.y <= H - 50:
                            self.click_editor(event.pos)

                elif event.type == pygame.TEXTINPUT:
                    if self.mode == MODE_EDIT:
                        mods = pygame.key.get_mods()
                        if not mods & pygame.KMOD_CTRL:
                            self.insert(event.text)
                    elif self.mode == MODE_NEWNAME:
                        if event.text not in '\\/:*?"<>|':
                            self.new_name += event.text

                elif event.type == pygame.KEYDOWN:
                    if self.mode == MODE_LIST:
                        self.handle_keydown_list(event)
                    elif self.mode == MODE_EDIT:
                        self.handle_keydown_edit(event)
                    elif self.mode == MODE_NEWNAME:
                        if event.key == pygame.K_ESCAPE:
                            self.mode = MODE_LIST
                        elif event.key == pygame.K_BACKSPACE:
                            self.new_name = self.new_name[:-1]
                        elif event.key == pygame.K_RETURN:
                            self.create_file(self.new_name)

            if self.mode == MODE_LIST:
                self.draw_list()
            elif self.mode == MODE_EDIT:
                self.draw_editor()
            else:
                self.draw_newname()
            pygame.display.flip()
            self.clock.tick(FPS)
        pygame.quit()


if __name__ == "__main__":
    App().run()
