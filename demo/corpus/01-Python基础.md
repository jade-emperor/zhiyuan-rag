---
doc_id: demo-bagu-python
source: demo
---

# 面试八股 · Python 基础

> 这是**演示语料**：内容是为 `project-rag` 的演示模式自写的面试题与参考答案，不涉及任何真实机构、
> 课程或版权材料。它的作用是让「入库 → 检索 → 带引用的回答」这条链在**不下载 4.4G 模型**
> 的情况下也能跑通；作者本机的真实语料在 `data/corpus/` 下（不进仓库，结构与这里一致）。

### 1. 可变类型与不可变类型有什么区别

不可变类型（`int` / `float` / `str` / `tuple` / `frozenset`）一旦创建就不能原地修改，
"改"其实是新建一个对象再重新绑定名字；可变类型（`list` / `dict` / `set`）可以原地增删改。

这条区别有两个实际后果：不可变对象**可以作为字典的键**（要求可哈希，且哈希值在生命周期内不变），
列表就不行；可变对象**当默认参数**会出事故 —— `def f(items=[])` 的默认值只在函数定义时创建一次，
多次调用会共享同一个列表，正确写法是 `def f(items=None)` 再在函数体里 `items = [] if items is None else items`。

### 2. 装饰器是什么，怎么写一个带参数的装饰器

装饰器是"接收一个函数、返回一个新函数"的可调用对象，作用是在不改被装饰函数代码的前提下，
在它前后插入逻辑（计时、日志、鉴权、重试）。

```python
import functools, time

def timed(prefix=""):
    def decorator(func):
        @functools.wraps(func)          # 保住 __name__ / __doc__，排障时很重要
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                print(f"{prefix}{func.__name__} {time.perf_counter() - start:.3f}s")
        return wrapper
    return decorator

@timed("retrieve:")
def search(q): ...
```

三层结构是关键：**最外层收装饰器自己的参数，中间层收被装饰的函数，最内层收调用参数**。
漏掉 `functools.wraps` 会让 `search.__name__` 变成 `wrapper`，日志和调试器里全是一样的名字。

### 3. 生成器和迭代器有什么区别

迭代器是实现了 `__iter__` 与 `__next__` 的对象；生成器是用 `yield` 写的函数，
调用它得到的就是一个迭代器 —— **生成器是造迭代器最省事的写法**。

价值在惰性：生成器一次只产出一个值，内存占用与数据量无关。读一个 10GB 的日志文件时，
`for line in open(path)` 不会把文件读进内存，而 `open(path).readlines()` 会。
另外 `yield from` 可以把子生成器委托出去，递归遍历树结构时比自己写栈清楚得多。

### 4. GIL 是什么，它对多线程意味着什么

GIL（全局解释器锁）保证同一时刻只有一个线程在执行 Python 字节码。
后果是：**纯 Python 计算**用多线程得不到加速，多核只能靠多进程（`multiprocessing`）或换语言实现。

但它不阻塞 I/O：线程执行阻塞式系统调用（网络、磁盘）时会释放 GIL，
所以"并发等网络"这种场景多线程仍然有效（爬虫、调 100 个接口）。
CPU 密集的数值计算则应该交给 NumPy / PyTorch 这类在 C 层释放 GIL 的库，
或者在 3.13 上试 `free-threading` 构建（GIL 可关，但生态兼容性要自己验）。

### 5. 深拷贝与浅拷贝的区别

浅拷贝（`copy.copy` / `list[:]` / `dict.copy`）只复制最外层容器，内部的元素仍是**同一个对象**；
深拷贝（`copy.deepcopy`）递归复制到底，两份数据完全独立。

典型事故：`rows = [{"a": 1}]` 做浅拷贝后修改 `rows2[0]["a"]`，原列表也变了。
判断标准很简单 —— 容器里装的是不可变对象时浅拷贝就够；装的是可变对象（dict / list / 自定义对象）
而你还要改它，就必须深拷贝。注意 `deepcopy` 有代价：对象图大时明显变慢，且循环引用要靠它的 `memo` 处理。

### 6. 异常处理里 finally 和 else 各自什么时候执行

`try` / `except` / `else` / `finally` 的执行顺序是：没有异常 → `else` → `finally`；
有异常且被捕获 → `except` → `finally`；异常没被捕获 → 先 `finally` 再把异常抛出去。

两个坑：① `finally` 里有 `return` 会**吞掉**正在抛出的异常，也让 `else` 的结果作废，
所以清理逻辑里不要写 `return`；② `except Exception` 要收窄到具体类型并记日志，
裸 `except:` 会连 `KeyboardInterrupt` 一起吞掉，让 Ctrl-C 失效。
