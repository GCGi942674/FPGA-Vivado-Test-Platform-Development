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

// ================= CONFIG =================

static const std::string WORK_DIR =
    "/home/user3/workspace/galaxcore";

static const std::string BIN_SRC =
    "/home/user3/workspace/galaxcore/bin/Linux_64/GalaxCore";

static const std::string BIN_DST =
    "/home/xiaonan/Share/zw_cache/Galaxcore_bin";

static const std::string HISTORY_LOG =
    BIN_DST + "/build_history.log";

static const std::string FAIL_LOG =
    BIN_DST + "/mk_fail";

static const std::string CHECKPOINT_FILE =
    BIN_DST + "/last_revision.txt";

static const std::string SVN_URL = "http://192.168.10.10/svn/galaxcore/galaxcore"

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
    return run_in_workdir("csh -c \"mk -j\"") == 0;
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

// ================= LOG INIT =================

void init_log()
{
    std::ofstream f(HISTORY_LOG, std::ios::app);

    f << "============================================================\n";
    f << "Starting monitor time: " << now() << "\n";
    f << "Starting version: r" << load_checkpoint() << "\n";
    f << "============================================================\n";
    f << "No.\tRevision\tAuthor\tTime\t\t\tResult\n";
    f << "------------------------------------------------------------\n";
}

// ================= HISTORY LOG =================

void log_row(int rev,
             const std::string &author,
             const std::string &time,
             const std::string &result)
{
    std::ofstream f(HISTORY_LOG, std::ios::app);

    build_no++;

    f << build_no << "\t"
      << "r" << rev << "\t"
      << author << "\t"
      << time << "\t"
      << result
      << "\n";
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

    svn_clean();

    if (!checkout(rev))
        return;

    // first build
    if (build())
    {
        copy_bin(rev);
        log_row(rev, "unknown", now(), "SUCCESS");
        save_checkpoint(rev);
        return;
    }

    // retry once after clean
    make_clean();

    if (build())
    {
        copy_bin(rev);
        log_row(rev, "unknown", now(), "SUCCESS_AFTER_CLEAN");
        save_checkpoint(rev);
        return;
    }

    log_row(rev, "unknown", now(), "FAIL");
    log_fail(rev);

    // IMPORTANT: always advance checkpoint to avoid stuck loop
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
            std::cout
                << "[CI] idle | local="
                << local
                << " head="
                << head
                << std::endl;

            sleep(10);
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