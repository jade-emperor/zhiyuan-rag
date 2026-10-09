---
doc_id: demo-bagu-db-linux
source: demo
---

# 面试八股 · 数据库与 Linux

> 自写的演示语料（同 `01-Python基础.md` 的说明）：题目与答案是通用技术知识，不是任何机构的材料。

### 7. 为什么 MySQL 用了索引还是慢

先看**有没有真的用上索引**：`EXPLAIN` 的 `type` 是 `ALL` 就是全表扫描，
`key` 为 `NULL` 说明优化器放弃了索引。常见原因有四类：

① **索引失效**：在索引列上做运算或函数（`where date(created_at) = '2026-01-01'`）、
隐式类型转换（字符串列传数字）、`like '%前缀'` 前置通配。
② **区分度太低**：性别、状态这类只有几个值的列，优化器判断走索引还不如全表扫。
③ **回表代价高**：查的列不在索引里，命中大量行时每行都要回聚簇索引取数据 ——
这时覆盖索引（把要查的列一起放进索引）能直接消掉回表。
④ **锁等待**：`SHOW PROCESSLIST` 里看到大量 `Waiting for table metadata lock` 或行锁等待，
那是并发写阻塞，不是索引问题。

### 8. 事务的四个隔离级别分别解决什么问题

读未提交（Read Uncommitted）什么都不解决，会读到别人未提交的脏数据；
读已提交（Read Committed）解决脏读，但同一事务里两次同样的查询可能得到不同结果（不可重复读）；
可重复读（Repeatable Read，MySQL InnoDB 默认）解决不可重复读，
靠 MVCC 在事务开始时取一个一致性快照；串行化（Serializable）加范围锁，解决幻读但并发最差。

InnoDB 在可重复读下用**间隙锁**（next-key lock）挡住"同一范围里新插入的行"，
所以它基本不会有幻读 —— 代价是范围更新容易锁住比预期更多的行，死锁概率上升。

### 9. 什么是回表，什么是覆盖索引

InnoDB 的主键索引（聚簇索引）叶子节点存的是整行数据；二级索引的叶子节点只存
**索引列 + 主键值**。所以按二级索引查 `select name, age from user where name = 'x'` 时，
先在 `name` 索引里找到主键，再拿主键去聚簇索引取整行 —— 这一步就是**回表**。

如果查询要的列都在索引里（`select name from user where name = 'x'`），
优化器就不用回表，这叫**覆盖索引**，`EXPLAIN` 的 `Extra` 会显示 `Using index`。
把高频查询涉及的列组合进一个联合索引，是最常见的优化手段。

### 10. 慢查询怎么定位

三步走：① 开慢查询日志（`slow_query_log=ON`、`long_query_time=1`），
用 `mysqldumpslow` 或 `pt-query-digest` 按**总耗时**排序找最值钱的那几条；
② 对目标 SQL 跑 `EXPLAIN`（必要时 `EXPLAIN ANALYZE`）看访问类型、扫描行数、
有没有 `Using filesort` / `Using temporary`；
③ 看 `SHOW STATUS LIKE 'Innodb_row_lock%'` 与 `information_schema.INNODB_TRX`
判断是不是锁等待，而不是 SQL 本身写得差。

注意"总耗时高"和"单次慢"要分开处理：单次慢查索引，频次高则考虑加缓存或改批量。

### 11. Linux 上怎么定位一个进程 CPU 高

`top` 找到 PID，再 `top -Hp <PID>` 看是哪个**线程**在烧 CPU，把线程 ID 转成十六进制，
用 `jstack` / `py-spy dump --pid <PID>` 打到具体调用栈。

如果是短时冲高、抓不到现场，用 `pidstat -u 1` 或 `perf top -p <PID>` 连续采样。
配合 `iostat -x 1`（磁盘）、`vmstat 1`（上下文切换、swap）、`ss -s`（连接数）
就能判断瓶颈是 CPU、IO 还是锁竞争 —— 别只看 CPU 一条线就下结论。

### 12. 磁盘满了但 du 看不出大文件，怎么查

先确认 `df -h` 里满的是哪个挂载点，然后查**被删除但仍被进程占用**的文件：

```bash
lsof +L1 | sort -k7 -n | tail     # 已删除但句柄没释放的文件
```

这类文件占着磁盘却不在目录树里，`du` 永远扫不到。常见来源是日志文件被 `rm` 后进程还在写，
或者容器里 `docker logs` 的输出。处理方式是重启对应进程，或者用 `truncate -s 0` 清空还在写的文件。
另外别忘了 `df -i`：inode 用尽也会报"空间不足"，而那时字节数可能还有富余。
