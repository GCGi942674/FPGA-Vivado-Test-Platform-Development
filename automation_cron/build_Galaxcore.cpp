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
#include <sys/wait.h>

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

bool run_in_workdir(const std::string &cmd)
{
    std::string full = "cd " + WORK_DIR + " && " + cmd + " > /dev/null 2>&1";
    int status = system(full.c_str());
    if (status == -1) {
        // fork/exec failed
        return false;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status) == 0;
    }
    // killed by signal
    return false;
}

// ================= SVN =================

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
    return run_in_workdir(
        "csh -c 'mk; exit $status'"
    );
}

void make_clean()
{
    run_in_workdir("make clean");
}

// ================= CHECKOUT =================

bool checkout(int rev)
{
    return run_in_workdir(
        "svn update -r " + std::to_string(rev)
    );
}

// ================= ZIP =================

bool compress_to_tar(int rev)
{
    struct stat st = {0};
    if (stat(TAR_DIR.c_str(), &st) == -1) {
        mkdir(TAR_DIR.c_str(), 0755);
    }

    std::string zipfile = TAR_DIR + "/Galaxcore_" + std::to_string(rev) + ".zip";
    std::string cmd = "zip -j " + zipfile + " " + BIN_SRC + " > /dev/null 2>&1";
    return system(cmd.c_str()) == 0;
}

void clean_old_tars()
{
    std::string cmd =
        "cd " + TAR_DIR +
        " && ls -1v Galaxcore_*.zip 2>/dev/null | head -n -" +
        std::to_string(MAX_BIN_KEEP) + " | xargs -r rm -f";
    system(cmd.c_str());
}

// ================= LOGGING =================

void init_log()
{
    std::ofstream f(HISTORY_LOG, std::ios::app);
    f << "============================================================\n";
    f << "Starting monitor time: " << now() << "\n";
    f << "Starting version: r" << load_checkpoint() << "\n";
    f << "============================================================\n";
    f << "Revision\tAuthor\t\tTime\t\t\tResult\n";
    f << "------------------------------------------------------------\n";
}

void log_row(int rev,
             const std::string &author,
             const std::string &time,
             const std::string &result)
{
    std::ofstream f(HISTORY_LOG, std::ios::app);
    f << "r" << rev << " | " << author << " | " << time << " | " << result << "\n";
    f << "------------------------------------------------------------\n";
}

void log_fail(int rev)
{
    std::ofstream f(FAIL_LOG, std::ios::app);
    f << "[" << now() << "] FAIL r" << rev << "\n";
}

// ================= BUILD FLOW =================

void build_revision(int rev)
{
    std::cout << "[CI] build r" << rev << std::endl;

    std::string author = get_author(rev);

    svn_clean();

    // 1. checkout
    if (!checkout(rev))
    {
        std::cerr << "[CI] svn update failed for r" << rev << std::endl;
        log_row(rev, author, now(), "failed (svn update)");
        log_fail(rev);
        save_checkpoint(rev);
        return;
    }

    // 2. first build attempt
    if (build())
    {
        if (!compress_to_tar(rev))
        {
            std::cerr << "[CI] compress failed for r" << rev << std::endl;
            log_row(rev, author, now(), "failed (compress)");
            log_fail(rev);
            save_checkpoint(rev);
            return;
        }
        clean_old_tars();
        log_row(rev, author, now(), "successful");
        save_checkpoint(rev);
        return;
    }

    // 3. retry after make clean
    make_clean();
    if (build())
    {
        if (!compress_to_tar(rev))
        {
            std::cerr << "[CI] compress failed for r" << rev << std::endl;
            log_row(rev, author, now(), "failed (compress)");
            log_fail(rev);
            save_checkpoint(rev);
            return;
        }
        clean_old_tars();
        log_row(rev, author, now(), "successful");
        save_checkpoint(rev);
        return;
    }

    // 4. build failed after retry
    log_row(rev, author, now(), "failed");
    log_fail(rev);
    save_checkpoint(rev);
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
            while (running)
            {
                int current_head = get_head();           // 实时获取
                if (current_head > local) break;         // 新版本出现，退出 idle

                int elapsed = static_cast<int>(time(nullptr) - wait_start);
                std::cout << "\r[CI] Idle | local=" << local
                        << " head=" << current_head    // 显示实时值
                        << " wait: " << elapsed << "s" << std::flush;
                sleep(1);
            }
            std::cout << std::endl;
            head = get_head();   // 更新外层 head，后续继续主循环
            continue;
        }

        std::cout << "[CI] update detected "
                  << local << " -> " << head << std::endl;

        for (int r = local + 1; r <= head && running; ++r)
        {
            build_revision(r);
        }

        sleep(2);
    }

    std::cout << "[CI] exit\n";
    return 0;
}