#include <iostream>
#include <fstream>
#include <string>
#include <cstdlib>
#include <cstdio>
#include <vector>
#include <algorithm>
#include <unistd.h>
#include <signal.h>
#include <ctime>
#include <iomanip>
#include <sys/stat.h> 

// ================= CONFIG =================

static const std::string WORK_DIR =
    "/home/user3/workspace/galaxcore";

static const std::string BIN_SRC =
    "/home/user3/workspace/galaxcore/bin/Linux_64/GalaxCore";

static const std::string BIN_DST =
    "/home/xiaonan/Share/zw_cache/Galaxcore_bin";

static const std::string TAR_DIR = BIN_DST + "/zip";

static const std::string HISTORY_LOG =
    BIN_DST + "/build_history.log";

static const std::string FAIL_LOG =
    BIN_DST + "/mk_fail";

static const std::string CHECKPOINT_FILE =
    BIN_DST + "/last_revision.txt";

static const std::string SVN_URL = "http://192.168.10.10/svn/galaxcore/galaxcore";

static const int MAX_BIN_KEEP = 150;

static bool running = true;
static int build_no = 0;

// ================= SIGNAL =================

void signal_handler(int)
{
    running = false;
    std::cout << "\n[CI] stopping safely...\n";
}

// ================= TIME =================

std::string now()
{
    time_t t = time(nullptr);
    char buf[64];
    strftime(buf, sizeof(buf), "%Y-%m-%d %H:%M:%S", localtime(&t));
    return std::string(buf);
}

// ================= CORE EXEC =================

// IMPORTANT: all build-related commands must go through WORK_DIR
int run_in_workdir(const std::string &cmd)
{
    std::string full =
        "cd " + WORK_DIR + " && " + cmd + " > /dev/null 2>&1";

    return system(full.c_str());
}

// ================= SVN =================

// remote HEAD (NOT in workspace)
int get_head()
{
    std::string cmd =
        "svn info " + SVN_URL +
        " --show-item revision";

    FILE *fp = popen(cmd.c_str(), "r");
    if (!fp) return -1;

    char buf[64] = {0};
    fgets(buf, sizeof(buf), fp);
    pclose(fp);

    return atoi(buf);
}

std::string get_author(int rev)
{
    std::string cmd =
        "svn info -r " + std::to_string(rev) + " " + SVN_URL +
        " --show-item last-changed-author";

    FILE *fp = popen(cmd.c_str(), "r");

    if (!fp)
        return "unknown";

    char buf[128] = {0};
    fgets(buf, sizeof(buf), fp);
    pclose(fp);

    std::string author(buf);

    // remove newline
    author.erase(std::remove(author.begin(), author.end(), '\n'), author.end());

    if (author.empty())
        return "unknown";

    return author;
}

// ================= CHECKPOINT =================

int load_checkpoint()
{
    std::ifstream f(CHECKPOINT_FILE);
    int v = 0;

    if (!(f >> v))
    {
        int head = get_head();
        return head > 0 ? head - 1 : 0;
    }

    return v;
}

void save_checkpoint(int v)
{
    std::ofstream f(CHECKPOINT_FILE);
    f << v;
}

// ================= SVN CLEAN =================

void svn_clean()
{
    run_in_workdir("svn revert -R .");
    run_in_workdir("svn cleanup");
}

// ================= BUILD =================

bool build()
{
    return run_in_workdir("csh -c \"mk\"") == 0;
}

void make_clean()
{
    run_in_workdir("make clean");
}

// ================= CHECKOUT REV =================

bool checkout(int rev)
{
    return run_in_workdir(
        "svn update -r " + std::to_string(rev)
    ) == 0;
}

// ================= COPY BIN =================

bool copy_bin(int rev)
{
    std::string dst =
        BIN_DST + "/Galaxcore_" + std::to_string(rev);

    return system(("cp " + BIN_SRC + " " + dst).c_str()) == 0;
}

// 将编译产物打包为 Galaxcore_<rev>.zip 并放入 tar 目录
bool compress_to_tar(int rev)
{
    // 确保 tar 目录存在
    struct stat st = {0};
    if (stat(TAR_DIR.c_str(), &st) == -1) {
        mkdir(TAR_DIR.c_str(), 0755);
    }

    std::string zipfile = TAR_DIR + "/Galaxcore_" + std::to_string(rev) + ".zip";
    // -j : 不保留目录结构，只存储文件本身
    std::string cmd = "zip -j " + zipfile + " " + BIN_SRC + " > /dev/null 2>&1";

    return system(cmd.c_str()) == 0;
}

// 保留最新的 MAX_BIN_KEEP 个 zip 文件，删除旧的
void clean_old_tars()
{
    std::string cmd =
        "cd " + TAR_DIR +
        " && ls -1v Galaxcore_*.zip 2>/dev/null | head -n -" +
        std::to_string(MAX_BIN_KEEP) + " | xargs -r rm -f";

    system(cmd.c_str());
}

// ================= LOG INIT =================

void init_log()
{
    std::ofstream f(HISTORY_LOG, std::ios::app);

    f << "============================================================\n";
    f << "Starting monitor time: " << now() << "\n";
    f << "Starting version: r" << load_checkpoint() << "\n";
    f << "============================================================\n";
    f << "No.\t\tRevision\t\tAuthor\t\tTime\t\t\tResult\n";
    f << "------------------------------------------------------------\n";
}

// ================= HISTORY LOG =================

void log_row(int rev,
             const std::string &author,
             const std::string &time,
             const std::string &result)
{
    std::ofstream f(HISTORY_LOG, std::ios::app);

    f << "r" << rev << " | " << author << " | " << time << " | " << result << "\n";
    f << "------------------------------------------------------------\n";
}

// ================= FAIL LOG =================

void log_fail(int rev)
{
    std::ofstream f(FAIL_LOG, std::ios::app);

    f << "==================================================\n";
    f << "[" << now() << "] FAIL r" << rev << "\n";
    f << "==================================================\n";
}

// ================= BUILD FLOW =================

void build_revision(int rev)
{
    std::cout << "[CI] build r" << rev << std::endl;

    std::string author = get_author(rev);

    svn_clean();

    if (!checkout(rev))
        return;

    // 第一次编译
    if (build())
    {
        if (compress_to_tar(rev)) {
            clean_old_tars();
        } else {
            std::cerr << "[CI] WARNING: compress failed for r" << rev << std::endl;
        }
        log_row(rev, author, now(), "SUCCESS");
        save_checkpoint(rev);
        return;
    }

    // 失败后 clean 再试一次
    make_clean();
    if (build())
    {
        if (compress_to_tar(rev)) {
            clean_old_tars();
        } else {
            std::cerr << "[CI] WARNING: compress failed for r" << rev << std::endl;
        }
        log_row(rev, author, now(), "SUCCESS");
        save_checkpoint(rev);
        return;
    }

    // 两次都失败 → 记录失败
    log_row(rev, author, now(), "FAILURE");   // 原为 SUCCESS，已修正
    log_fail(rev);
    save_checkpoint(rev);                     // 仍然推进版本，避免死循环
}

// ================= MAIN LOOP =================

int main()
{
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);

    init_log();

    chdir(BIN_DST.c_str());

    std::cout << "[CI] SVN build watcher started\n";

    while (running)
    {
        int head = get_head();
        int local = load_checkpoint();

        if (head < 0)
        {
            sleep(10);
            continue;
        }

        if (head <= local)
        {
            time_t wait_start = time(nullptr);

            // Idle loop: update display every second, exit when new revision appears
            while (running && get_head() <= local)
            {
                int elapsed = static_cast<int>(time(nullptr) - wait_start);
                std::cout << "\r[CI] Idle | local=" << local
                        << " head=" << head
                        << " wait: " << elapsed << "s" << std::flush;
                sleep(1);
            }
            std::cout << std::endl;  // newline after idle ends

            // Refresh head for the outer loop
            head = get_head();
            continue;
        }

        std::cout
            << "[CI] update detected "
            << local << " -> " << head
            << std::endl;

        for (int r = local + 1; r <= head && running; ++r)
        {
            build_revision(r);
        }

        sleep(2);
    }

    std::cout << "[CI] exit\n";
    return 0;
}