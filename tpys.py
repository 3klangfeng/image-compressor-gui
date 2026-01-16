import os
import shutil
import threading
import time
import gc
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from PIL import Image
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import ttkbootstrap as tb
from ttkbootstrap.constants import *

class ImageCompressorCore:
    def __init__(self, target_size_kb=100, backup=False, auto_resize=True, log_func=None, max_dimension=0):
        self.target_size_kb = target_size_kb
        self.backup = backup
        self.auto_resize = auto_resize
        self.log = log_func or print
        self.max_dimension = max_dimension
        self.supported_formats = ('JPEG', 'JPG', 'PNG', 'WEBP', 'BMP', 'GIF', 'TIFF')
        self.MIN_Q = 10
        self.RESIZE_STEP = 0.9
        self.MIN_DIMENSION = 50
        self.MAX_ATTEMPTS = 5  # 最大尝试次数

    def _clear_readonly(self, path):
        """清除只读权限，确保文件可写"""
        try:
            if os.path.exists(path) and not os.access(path, os.W_OK):
                os.chmod(path, 0o666)
            return True
        except Exception as e:
            self.log(f"    ⚠️ 权限修改失败: {e}")
            return False
    
    def _safe_replace(self, src, dst):
        """多次重试安全替换文件"""
        for attempt in range(5):
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                os.replace(src, dst)
                return True
            except Exception as e:
                if attempt < 4:
                    time.sleep(0.2)
                else:
                    self.log(f"    ⚠️ 文件替换失败: {e}")
        return False

    def _convert_to_rgb(self, img):
        """强制转换为RGB格式，处理所有颜色空间"""
        try:
            if img.mode == 'RGBA':
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[3])
                return bg
            elif img.mode == 'LA':
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[1])
                return bg
            elif img.mode == 'P':
                if 'transparency' in img.info:
                    img = img.convert('RGBA')
                    bg = Image.new('RGB', img.size, (255, 255, 255))
                    bg.paste(img, mask=img.split()[3])
                    return bg
                else:
                    return img.convert('RGB')
            elif img.mode in ('L', '1', 'PA'):
                return img.convert('RGB')
            elif img.mode != 'RGB':
                return img.convert('RGB')
            return img
        except Exception as e:
            self.log(f"    ⚠️ 颜色转换失败: {e}")
            return img.convert('RGB') if img.mode != 'RGB' else img

    def _binary_compress(self, img, tmp_path, ceiling_kb):
        """二分查找最优质量值"""
        low, high = self.MIN_Q, 95
        best_q = 95
        found_size = float('inf')
        
        while low <= high:
            mid = (low + high) // 2
            try:
                img.save(tmp_path, quality=mid, optimize=True, format='JPEG')
                size = os.path.getsize(tmp_path) / 1024
            except Exception as e:
                self.log(f"    ⚠️ 压缩尝试失败 (Q={mid}): {e}")
                size = float('inf')
            
            if size < found_size and size < ceiling_kb:
                found_size, best_q = size, mid
            
            if size > self.target_size_kb:
                high = mid - 1
            else:
                low = mid + 1
        
        # 保存最终结果
        try:
            img.save(tmp_path, quality=max(self.MIN_Q, high), optimize=True, format='JPEG')
            final_s = os.path.getsize(tmp_path) / 1024
            return final_s, final_s <= self.target_size_kb * 1.05
        except Exception as e:
            self.log(f"    ⚠️ 最终压缩失败: {e}")
            return float('inf'), False

    def process_single_image(self, path):
        """处理单张图片 - 完整流程"""
        tmp_file = path + ".tmp"
        img = None
        
        try:
            # 步骤1: 权限检查
            if not self._clear_readonly(path):
                self.log(f"❌ 权限错误: {os.path.basename(path)}")
                return
            
            # 步骤2: 获取原始信息
            try:
                init_s = os.path.getsize(path) / 1024
            except:
                self.log(f"❌ 无法读取文件: {os.path.basename(path)}")
                return
            
            # 步骤3: 打开图片（支持所有格式）
            try:
                with open(path, 'rb') as f:
                    img = Image.open(f)
                    img.load()
                    original_format = img.format
            except Exception as e:
                self.log(f"❌ 打开失败: {os.path.basename(path)} > {e}")
                return
            
            # 步骤4: 强制RGB转换
            img = self._convert_to_rgb(img)
            
            # 步骤5: 尺寸限制处理
            w, h = img.size
            is_resized = False
            if self.max_dimension > 0 and max(w, h) > self.max_dimension:
                ratio = self.max_dimension / max(w, h)
                new_w, new_h = int(w * ratio), int(h * ratio)
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                is_resized = True
                self.log(f"    📏 尺寸缩放: {w}x{h} → {new_w}x{new_h}")
            
            # 步骤6: 判断是否需要处理
            is_jpg = path.lower().endswith(('.jpg', '.jpeg'))
            if is_jpg and not is_resized and init_s <= self.target_size_kb:
                self.log(f"⭐ 已达标: {os.path.basename(path)} ({init_s:.1f}KB)")
                return
            
            # 步骤7: 创建备份（可选）
            if self.backup and not os.path.exists(path + ".bak"):
                try:
                    shutil.copy2(path, path + ".bak")
                except Exception as e:
                    self.log(f"    ⚠️ 备份失败: {e}")
            
            # 步骤8: 压缩处理（自适应）
            curr_img = img.copy()
            attempt = 0
            
            while attempt < self.MAX_ATTEMPTS:
                attempt += 1
                ceil = float('inf') if (is_resized or not is_jpg) else init_s
                fs, ok = self._binary_compress(curr_img, tmp_file, ceil)
                
                if ok:  # 达到目标
                    break
                
                if not self.auto_resize:  # 不自动缩放，接受当前大小
                    break
                
                # 继续缩放
                nw = int(curr_img.size[0] * self.RESIZE_STEP)
                nh = int(curr_img.size[1] * self.RESIZE_STEP)
                
                if nw < self.MIN_DIMENSION or nh < self.MIN_DIMENSION:
                    self.log(f"    ⚠️ 已达最小尺寸 ({nw}x{nh}), 停止缩放")
                    break
                
                curr_img = curr_img.resize((nw, nh), Image.Resampling.LANCZOS)
                self.log(f"    ➡️ 继续缩小至 {nw}x{nh}...")
            
            # 步骤9: 输出为JPG
            out_path = os.path.splitext(path)[0] + '.jpg'
            
            # 步骤10: 安全保存最终文件
            if self._safe_replace(tmp_file, out_path):
                final_s = os.path.getsize(out_path) / 1024
                reduction = ((init_s - final_s) / init_s * 100) if init_s > 0 else 0
                self.log(f"✅ 完成: {os.path.basename(out_path)} | {final_s:.1f}KB ↓{reduction:.1f}%")
                
                # 步骤11: 删除原文件（所有格式都删除）
                if path != out_path:
                    for attempt in range(3):
                        try:
                            if os.path.exists(path):
                                os.remove(path)
                                self.log(f"    🗑️ 已删除原文件: {os.path.basename(path)}")
                                break
                        except Exception as e:
                            if attempt < 2:
                                time.sleep(0.1)
                            else:
                                self.log(f"    ⚠️ 删除原文件失败: {e}")
            else:
                self.log(f"❌ 保存失败: {os.path.basename(path)}")
        
        except Exception as e:
            self.log(f"❌ 异常: {os.path.basename(path)} > {str(e)[:50]}")
        
        finally:
            # 步骤12: 清理资源
            if img:
                img.close()
            
            # 强制删除临时文件（重试机制）
            for attempt in range(3):
                try:
                    if os.path.exists(tmp_file):
                        os.remove(tmp_file)
                        break
                except:
                    time.sleep(0.1)
            
            # 主动垃圾回收，防止内存泄漏
            gc.collect()


class ImageCompressorUI:
    def __init__(self, root):
        self.root = root
        self.root.title("🚀 图片压缩助手 - Pro版 (稳定版)")
        
        cpu_cores = os.cpu_count() or 4
        recommended_threads = max(1, min(cpu_cores - 1, 8))  # 限制最多8个线程
        
        self.folder_path = tk.StringVar()
        self.target_size = tk.IntVar(value=100)
        self.backup = tk.BooleanVar(value=False)
        self.auto_resize = tk.BooleanVar(value=True)
        self.max_workers = tk.IntVar(value=recommended_threads)
        self.max_dimension = tk.IntVar(value=1080)
        
        self.running = False
        self.image_list = []
        self.processed_count = 0
        self.failed_count = 0
        self.log_queue = Queue()
        
        self.setup_ui()
        self.center_window(self.root, 700, 650)
        self.root.after(100, self.refresh_logs)

    def center_window(self, win, w, h, parent=None):
        """窗口居中显示"""
        if parent:
            x = parent.winfo_x() + (parent.winfo_width() // 2) - (w // 2)
            y = parent.winfo_y() + (parent.winfo_height() // 2) - (h // 2)
        else:
            x = (win.winfo_screenwidth() // 2) - (w // 2)
            y = (win.winfo_screenheight() // 2) - (h // 2)
        win.geometry(f'{w}x{h}+{x}+{y}')

    def setup_ui(self):
        """构建UI界面"""
        # 顶部: 路径选择
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill=X)
        ttk.Label(top, text="路径:", font=("微软雅黑", 9, "bold")).pack(side=LEFT)
        ttk.Entry(top, textvariable=self.folder_path, state="readonly").pack(side=LEFT, fill=X, expand=True, padx=5)
        tb.Button(top, text="选择", command=self.on_select_btn, bootstyle=PRIMARY).pack(side=LEFT)

        # 中部: 核心配置
        cfg = ttk.Labelframe(self.root, text="核心配置 (参数实时生效)", padding=10)
        cfg.pack(fill=X, padx=10, pady=5)
        
        opts = [
            ("目标大小(KB):", self.target_size, 0, 0),
            ("长边限制(Px):", self.max_dimension, 1, 0),
            ("线程数:", self.max_workers, 2, 0)
        ]
        
        for label, var, row, col in opts:
            ttk.Label(cfg, text=label, font=("微软雅黑", 9)).grid(row=row, column=0, sticky=W, pady=3)
            ttk.Entry(cfg, textvariable=var, width=10, font=("微软雅黑", 9)).grid(row=row, column=1, sticky=W, padx=5)
        
        ttk.Checkbutton(cfg, text="自动缩放", variable=self.auto_resize, bootstyle="round-toggle").grid(row=0, column=2, padx=5)
        ttk.Checkbutton(cfg, text="备份原图", variable=self.backup, bootstyle="round-toggle").grid(row=1, column=2, padx=5)

        # 统计信息
        info_frm = ttk.Frame(self.root, padding=10)
        info_frm.pack(fill=X, padx=10)
        self.info_label = ttk.Label(info_frm, text="待扫描文件...", font=("微软雅黑", 9))
        self.info_label.pack(side=LEFT)
        self.progress_label = ttk.Label(info_frm, text="进度: 0/0", font=("微软雅黑", 9, "bold"))
        self.progress_label.pack(side=RIGHT)

        # 日志区域
        log_frm = ttk.Labelframe(self.root, text="执行日志 (实时状态监控)", padding=10)
        log_frm.pack(fill=BOTH, expand=True, padx=10, pady=5)
        
        self.log_area = tk.Text(log_frm, background="#1e1e1e", foreground="#dcdcdc",
                                font=("Consolas", 8), state=DISABLED, wrap=WORD, height=12)
        self.log_area.pack(fill=BOTH, expand=True, side=LEFT)
        
        scrollbar = ttk.Scrollbar(log_frm, orient=VERTICAL, command=self.log_area.yview)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.log_area.config(yscrollcommand=scrollbar.set)

        # 底部: 按钮
        btm = ttk.Frame(self.root, padding=10)
        btm.pack(fill=X)
        
        self.run_btn = tb.Button(btm, text="🚀 启动处理", width=15, bootstyle=SUCCESS, command=self.start_work)
        self.run_btn.pack(side=LEFT, padx=5)
        
        tb.Button(btm, text="清空日志", width=10, bootstyle=INFO, command=self.clear_logs).pack(side=LEFT, padx=5)
        tb.Button(btm, text="退出", width=8, bootstyle=DANGER, command=self.on_exit).pack(side=RIGHT)

    def on_select_btn(self):
        """打开文件选择对话框"""
        pop = tb.Toplevel(self.root)
        pop.title("选择模式")
        self.center_window(pop, 380, 130, self.root)
        pop.grab_set()
        
        f = ttk.Frame(pop, padding=15)
        f.pack(expand=True)
        
        ttk.Label(f, text="选择处理模式:", font=("微软雅黑", 10, "bold")).pack(pady=10)
        
        btn_frm = ttk.Frame(f)
        btn_frm.pack()
        
        tb.Button(btn_frm, text="📁 文件夹", command=lambda: [pop.destroy(), self.load('dir')], width=12).pack(side=LEFT, padx=5)
        tb.Button(btn_frm, text="🖼️ 多图片", command=lambda: [pop.destroy(), self.load('files')], width=12).pack(side=LEFT, padx=5)

    def load(self, mode):
        """扫描图片文件"""
        self.image_list = []
        
        if mode == 'dir':
            p = filedialog.askdirectory(title="选择图片文件夹")
            if p:
                self.folder_path.set(p)
                self.log(f"🔍 正在扫描: {p}")
                
                for root, dirs, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif', '.tiff')):
                            if not f.lower().endswith('.bak'):
                                self.image_list.append(os.path.join(root, f))
        else:
            ps = filedialog.askopenfilenames(
                title="选择图片文件",
                filetypes=[("所有图片", "*.jpg *.jpeg *.png *.webp *.bmp *.gif *.tiff")]
            )
            if ps:
                self.folder_path.set(f"已选 {len(ps)} 文件")
                self.image_list = list(ps)
        
        if self.image_list:
            self.show_scan_report()
        else:
            self.log("⚠️ 未找到任何图片文件")

    def show_scan_report(self):
        """显示扫描报告"""
        if not self.image_list:
            return
        
        stats = Counter([os.path.splitext(p)[1].lower() for p in self.image_list])
        
        self.log("=" * 40)
        self.log(f"📊 扫描完成 | 共找到 {len(self.image_list)} 张图片")
        self.log("-" * 40)
        
        for ext, count in sorted(stats.items()):
            self.log(f"  {ext[1:].upper():6s}: {count:5d} 张")
        
        self.log("=" * 40)
        self.info_label.config(text=f"已扫描 {len(self.image_list)} 张图片")

    def log(self, m):
        """线程安全的日志记录"""
        self.log_queue.put(m)

    def clear_logs(self):
        """清空日志区"""
        self.log_area.config(state=NORMAL)
        self.log_area.delete(1.0, END)
        self.log_area.config(state=DISABLED)

    def refresh_logs(self):
        """异步刷新日志显示"""
        try:
            while True:
                m = self.log_queue.get_nowait()
                self.log_area.config(state=NORMAL)
                self.log_area.insert(END, m + "\n")
                self.log_area.see(END)
                self.log_area.config(state=DISABLED)
        except Empty:
            pass
        
        self.root.after(100, self.refresh_logs)

    def start_work(self):
        """启动处理任务"""
        if not self.image_list:
            messagebox.showwarning("警告", "请先选择图片！")
            return
        
        if self.running:
            messagebox.showwarning("提示", "处理中，请等待...")
            return
        
        # 参数验证
        try:
            target_size = self.target_size.get()
            max_dim = self.max_dimension.get()
            workers = self.max_workers.get()
            
            if target_size <= 0:
                messagebox.showerror("错误", "目标大小必须 > 0")
                return
            if workers <= 0 or workers > 16:
                messagebox.showerror("错误", "线程数应在 1-16 之间")
                return
        except:
            messagebox.showerror("错误", "参数设置有误！")
            return
        
        self.running = True
        self.processed_count = 0
        self.failed_count = 0
        self.run_btn.config(state=DISABLED)
        self.log(f"\n🚀 开始处理 {len(self.image_list)} 张图片...\n")
        
        threading.Thread(target=self.thread_pool_run, daemon=True).start()

    def thread_pool_run(self):
        """多线程处理"""
        start_time = time.time()
        
        core = ImageCompressorCore(
            self.target_size.get(),
            self.backup.get(),
            self.auto_resize.get(),
            self.log,
            self.max_dimension.get()
        )
        
        with ThreadPoolExecutor(max_workers=self.max_workers.get()) as pool:
            futures = []
            for p in self.image_list:
                future = pool.submit(self.wrapper, p, core)
                futures.append(future)
            
            # 监控进度
            completed = 0
            for future in futures:
                try:
                    future.result(timeout=30)
                except Exception as e:
                    self.log(f"⚠️ 线程异常: {str(e)[:50]}")
                    self.failed_count += 1
                
                completed += 1
                progress = f"进度: {completed}/{len(self.image_list)}"
                self.progress_label.config(text=progress)
                self.root.update_idletasks()
        
        # 完成统计
        elapsed = time.time() - start_time
        success = self.processed_count
        failed = len(self.image_list) - success
        
        self.log("\n" + "=" * 40)
        self.log(f"✨ 处理完成！")
        self.log(f"  成功: {success} | 失败: {failed}")
        self.log(f"  耗时: {elapsed:.1f}秒")
        self.log(f"  速度: {len(self.image_list)/elapsed:.1f} 张/秒")
        self.log("=" * 40 + "\n")
        
        self.running = False
        self.root.after(0, lambda: self.run_btn.config(state=NORMAL))

    def wrapper(self, p, core):
        """包装函数，用于计数"""
        try:
            core.process_single_image(p)
            self.processed_count += 1
        except Exception as e:
            self.log(f"⚠️ 处理异常: {str(e)[:50]}")

    def on_exit(self):
        """退出程序"""
        if self.running:
            if messagebox.askyesno("确认", "处理中，确定要退出吗？"):
                self.running = False
                self.root.quit()
        else:
            self.root.quit()


if __name__ == "__main__":
    # 允许处理超大图片
    Image.MAX_IMAGE_PIXELS = None
    
    # 禁用PNG压缩（加快速度）
    Image.LOAD_TRUNCATED_IMAGES = True
    
    app = ImageCompressorUI(tb.Window(themename="minty"))
    app.root.mainloop()