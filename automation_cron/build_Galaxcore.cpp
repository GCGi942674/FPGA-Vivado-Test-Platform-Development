#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dirent.h>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <regex>
#include <sstream>
#include <string>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace {

volatile std::sig_atomic_t g_running = 1;

std::string get_env_string(const char *name, const std::string &default_value)
{
    const char *value = std::getenv(name);
    return value != NULL && *value != '\0' ? std::string(value) : default_value;
}

int get_env_int(const char *name, int default_value)
{
    const char *value = std::getenv(name);
    if (value == NULL || *value == '\0') {
        return default_value;
    }

    char *end = NULL;
    errno = 0;
    const long parsed = std::strtol(value, &end, 10);
    if (errno != 0 || end == value || *end != '\0') {
        return default_value;
    }
    return static_cast<int>(parsed);
}

bool get_env_bool(const char *name, bool default_value)
{
    const char *value = std::getenv(name);
    if (value == NULL || *value == '\0') {
        return default_value;
    }
    return std::string(value) != "0";
}

std::string join_path(const std::string &left, const std::string &right)
{
    if (left.empty()) {
        return right;
    }
    if (right.empty()) {
        return left;
    }
    if (left[left.size() - 1] == '/') {
        return left + right;
    }
    return left + "/" + right;
}

struct Config {
    std::string work_dir;
    std::string bin_src;
    std::string bin_dst;
    std::string zip_dir;
    std::string mk_fail_file;
    std::string last_version_file;
    std::string legacy_last_revision_file;
    std::string svn_url;
    std::string zip_prefix;
    int max_bin_keep;
    int poll_interval;
    int idle_sleep;
    bool quiet_cmd_output;
    bool verbose_output;
    std::string submit_test_dir;
    std::string submit_test_script;
    std::string submit_success_marker;
    int author_width;
    int reason_width;

    Config()
        : work_dir(get_env_string(
              "GALAXCORE_WORK_DIR",
              "/home/user3/workspace/galaxcore")),
          bin_src(get_env_string(
              "GALAXCORE_BIN_SRC",
              join_path(work_dir, "bin/Linux_64/GalaxCore"))),
          bin_dst(get_env_string(
              "GALAXCORE_BIN_DST",
              "/home/xiaonan/Share/zw_cache/GalaxCore_bin")),
          zip_dir(get_env_string(
              "GALAXCORE_ZIP_DIR",
              join_path(bin_dst, "zip"))),
          mk_fail_file(get_env_string(
              "GALAXCORE_MK_FAIL_FILE",
              join_path(bin_dst, "mk_fail"))),
          last_version_file(get_env_string(
              "GALAXCORE_LAST_VERSION_FILE",
              join_path(bin_dst, "last_version"))),
          legacy_last_revision_file(join_path(bin_dst, "last_revision.txt")),
          svn_url(get_env_string(
              "GALAXCORE_SVN_URL",
              "http://192.168.10.10/svn/galaxcore/galaxcore")),
          zip_prefix(get_env_string("GALAXCORE_ZIP_PREFIX", "GalaxCore")),
          max_bin_keep(get_env_int("GALAXCORE_MAX_BIN_KEEP", 150)),
          poll_interval(get_env_int("GALAXCORE_POLL_INTERVAL", 2)),
          idle_sleep(get_env_int("GALAXCORE_IDLE_SLEEP", 1)),
          quiet_cmd_output(get_env_bool("GALAXCORE_QUIET", true)),
          verbose_output(get_env_bool("GALAXCORE_VERBOSE", false)),
          submit_test_dir(get_env_string(
              "GALAXCORE_SUBMIT_TEST_DIR",
              join_path(work_dir, "test2"))),
          submit_test_script(get_env_string(
              "GALAXCORE_SUBMIT_TEST_SCRIPT",
              "./submit_test.sh")),
          submit_success_marker(get_env_string(
              "GALAXCORE_SUBMIT_SUCCESS_MARKER",
              "No Case Fail, You can submit your code now~")),
          author_width(get_env_int("GALAXCORE_MK_FAIL_AUTHOR_WIDTH", 16)),
          reason_width(get_env_int("GALAXCORE_MK_FAIL_REASON_WIDTH", 28))
    {
    }
};

const Config CONFIG;

std::string trim(const std::string &text)
{
    const std::string whitespace = " \t\r\n";
    const std::string::size_type begin = text.find_first_not_of(whitespace);
    if (begin == std::string::npos) {
        return "";
    }
    const std::string::size_type end = text.find_last_not_of(whitespace);
    return text.substr(begin, end - begin + 1);
}

std::string to_lower(std::string text)
{
    std::transform(
        text.begin(),
        text.end(),
        text.begin(),
        [](unsigned char ch) { return static_cast<char>(std::tolower(ch)); });
    return text;
}

std::string to_upper(std::string text)
{
    std::transform(
        text.begin(),
        text.end(),
        text.begin(),
        [](unsigned char ch) { return static_cast<char>(std::toupper(ch)); });
    return text;
}

std::string now_string()
{
    const std::time_t current = std::time(NULL);
    std::tm local_tm;
    std::memset(&local_tm, 0, sizeof(local_tm));
    localtime_r(&current, &local_tm);

    char buffer[64];
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%d %H:%M:%S", &local_tm);
    return std::string(buffer);
}

void ci_log(const std::string &message)
{
    std::cout << "[CI] " << message << std::endl;
}

void ci_debug(const std::string &message)
{
    if (CONFIG.verbose_output) {
        std::cout << "[CI] " << message << std::endl;
    }
}

void signal_handler(int)
{
    g_running = 0;
    const char message[] = "\n[CI] stopping safely...\n";
    ::write(STDOUT_FILENO, message, sizeof(message) - 1);
}

bool path_exists(const std::string &path)
{
    struct stat st;
    return ::stat(path.c_str(), &st) == 0;
}

bool is_directory(const std::string &path)
{
    struct stat st;
    return ::stat(path.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
}

std::string parent_path(const std::string &path)
{
    const std::string::size_type pos = path.find_last_of('/');
    if (pos == std::string::npos) {
        return ".";
    }
    if (pos == 0) {
        return "/";
    }
    return path.substr(0, pos);
}

bool mkdir_p(const std::string &path, mode_t mode = 0755)
{
    if (path.empty() || path == ".") {
        return true;
    }
    if (is_directory(path)) {
        return true;
    }

    std::string current;
    if (path[0] == '/') {
        current = "/";
    }

    std::stringstream stream(path);
    std::string part;
    while (std::getline(stream, part, '/')) {
        if (part.empty()) {
            continue;
        }

        if (!current.empty() && current[current.size() - 1] != '/') {
            current += '/';
        }
        current += part;

        if (::mkdir(current.c_str(), mode) != 0 && errno != EEXIST) {
            return false;
        }
    }
    return is_directory(path);
}

bool atomic_write(const std::string &path, const std::string &content)
{
    if (!mkdir_p(parent_path(path))) {
        return false;
    }

    const std::string tmp_path = path + ".tmp";
    {
        std::ofstream output(tmp_path.c_str(), std::ios::out | std::ios::trunc);
        if (!output) {
            return false;
        }
        output << content;
        output.flush();
        if (!output) {
            output.close();
            ::unlink(tmp_path.c_str());
            return false;
        }
    }

    if (::rename(tmp_path.c_str(), path.c_str()) != 0) {
        ::unlink(tmp_path.c_str());
        return false;
    }
    return true;
}

std::vector<std::string> read_nonempty_lines(const std::string &path)
{
    std::vector<std::string> lines;
    std::ifstream input(path.c_str());
    if (!input) {
        return lines;
    }

    std::string line;
    while (std::getline(input, line)) {
        if (!trim(line).empty()) {
            lines.push_back(line);
        }
    }
    return lines;
}

bool write_lines(const std::string &path, const std::vector<std::string> &lines)
{
    std::ostringstream content;
    for (std::size_t i = 0; i < lines.size(); ++i) {
        content << lines[i] << '\n';
    }
    return atomic_write(path, content.str());
}

std::string shell_quote(const std::string &text)
{
    std::string quoted = "'";
    for (std::size_t i = 0; i < text.size(); ++i) {
        if (text[i] == '\'') {
            quoted += "'\\''";
        } else {
            quoted += text[i];
        }
    }
    quoted += "'";
    return quoted;
}

int decode_wait_status(int status)
{
    if (status == -1) {
        return -1;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return -1;
}

struct CommandResult {
    bool ok;
    int return_code;
    std::string output;

    CommandResult(bool success, int code, const std::string &text)
        : ok(success), return_code(code), output(text)
    {
    }
};

CommandResult run_command(
    const std::string &command,
    const std::string &cwd,
    bool capture_output,
    bool quiet)
{
    std::string full_command;
    if (!cwd.empty()) {
        full_command += "cd " + shell_quote(cwd) + " && ";
    }
    full_command += command;

    if (capture_output) {
        full_command += " 2>&1";
        FILE *pipe = ::popen(full_command.c_str(), "r");
        if (pipe == NULL) {
            return CommandResult(false, -1, std::strerror(errno));
        }

        std::string output;
        char buffer[4096];
        while (std::fgets(buffer, sizeof(buffer), pipe) != NULL) {
            output += buffer;
        }

        const int return_code = decode_wait_status(::pclose(pipe));
        return CommandResult(return_code == 0, return_code, output);
    }

    if (quiet) {
        full_command += " > /dev/null 2>&1";
    }

    const int return_code = decode_wait_status(::system(full_command.c_str()));
    return CommandResult(return_code == 0, return_code, "");
}

CommandResult run_in_workdir(
    const std::string &command,
    bool capture_output = false,
    bool quiet = CONFIG.quiet_cmd_output)
{
    return run_command(command, CONFIG.work_dir, capture_output, quiet);
}

std::string strip_ansi(const std::string &text)
{
    static const std::regex ansi_pattern("\\x1B\\[[0-9;?]*[ -/]*[@-~]");
    return std::regex_replace(text, ansi_pattern, "");
}

bool parse_prefixed_integer(
    const std::string &output,
    const std::string &prefix,
    int *value)
{
    std::istringstream stream(output);
    std::string line;
    while (std::getline(stream, line)) {
        if (line.compare(0, prefix.size(), prefix) != 0) {
            continue;
        }

        const std::string number = trim(line.substr(prefix.size()));
        char *end = NULL;
        errno = 0;
        const long parsed = std::strtol(number.c_str(), &end, 10);
        if (errno == 0 && end != number.c_str() && *end == '\0') {
            *value = static_cast<int>(parsed);
            return true;
        }
    }
    return false;
}

std::string parse_prefixed_text(
    const std::string &output,
    const std::string &prefix)
{
    std::istringstream stream(output);
    std::string line;
    while (std::getline(stream, line)) {
        if (line.compare(0, prefix.size(), prefix) == 0) {
            return trim(line.substr(prefix.size()));
        }
    }
    return "";
}

int get_head()
{
    const std::string command =
        "svn info " + shell_quote(CONFIG.svn_url);
    const CommandResult result = run_command(command, "", true, false);
    if (!result.ok) {
        return -1;
    }

    int revision = -1;
    if (parse_prefixed_integer(result.output, "Revision:", &revision)) {
        return revision;
    }
    return -1;
}

std::string get_author(int revision)
{
    const std::string command =
        "svn info -r " + std::to_string(revision) + " " +
        shell_quote(CONFIG.svn_url);
    const CommandResult result = run_command(command, "", true, false);
    if (result.ok) {
        const std::string author =
            parse_prefixed_text(result.output, "Last Changed Author:");
        if (!author.empty()) {
            return author;
        }
    }

    const std::string fallback_command =
        "svn log -r " + std::to_string(revision) + " --limit 1 " +
        shell_quote(CONFIG.svn_url);
    const CommandResult fallback =
        run_command(fallback_command, "", true, false);
    if (fallback.ok) {
        std::istringstream stream(fallback.output);
        std::string line;
        while (std::getline(stream, line)) {
            if (line.empty() || line[0] != 'r') {
                continue;
            }

            const std::string::size_type first = line.find('|');
            const std::string::size_type second =
                first == std::string::npos ? std::string::npos : line.find('|', first + 1);
            if (first != std::string::npos && second != std::string::npos) {
                const std::string author = trim(line.substr(first + 1, second - first - 1));
                if (!author.empty()) {
                    return author;
                }
            }
        }
    }
    return "unknown";
}

void svn_clean()
{
    run_in_workdir("svn revert -R .");
    run_in_workdir("svn cleanup");
}

bool checkout(int revision)
{
    const CommandResult result = run_in_workdir(
        "svn update -r " + std::to_string(revision));
    return result.ok;
}

bool read_integer_file(const std::string &path, int *value)
{
    std::ifstream input(path.c_str());
    int parsed = 0;
    if (!(input >> parsed)) {
        return false;
    }
    *value = parsed;
    return true;
}

int load_last_version()
{
    int value = 0;
    if (read_integer_file(CONFIG.last_version_file, &value)) {
        return value;
    }
    if (read_integer_file(CONFIG.legacy_last_revision_file, &value)) {
        return value;
    }

    const int head = get_head();
    return head > 0 ? head - 1 : 0;
}

bool save_last_version(int version)
{
    return atomic_write(
        CONFIG.last_version_file,
        std::to_string(version) + "\n");
}

bool build()
{
    return run_in_workdir("csh -c 'mk; exit $status'").ok;
}

void make_clean()
{
    run_in_workdir("make clean");
}

struct SubmitResult {
    bool ok;
    std::string reason;
    std::string output;

    SubmitResult(
        bool success,
        const std::string &result_reason,
        const std::string &result_output)
        : ok(success), reason(result_reason), output(result_output)
    {
    }
};

SubmitResult run_submit_test()
{
    if (!is_directory(CONFIG.submit_test_dir)) {
        return SubmitResult(
            false,
            "submit_test_dir_missing",
            "missing: " + CONFIG.submit_test_dir);
    }

    const std::string command =
        "csh -c " + shell_quote(CONFIG.submit_test_script + "; exit $status");
    CommandResult result = run_command(
        command,
        CONFIG.submit_test_dir,
        true,
        false);
    result.output = strip_ansi(result.output);

    if (result.output.find(CONFIG.submit_success_marker) != std::string::npos) {
        return SubmitResult(true, "submit_success", result.output);
    }

    const std::string reason = result.ok
        ? "submit_output_check_failed"
        : "submit_exit_" + std::to_string(result.return_code);
    return SubmitResult(false, reason, result.output);
}

std::string summarize_submit_output(const std::string &raw_output)
{
    const std::string output = strip_ansi(raw_output);
    std::vector<std::string> lines;
    std::vector<std::string> interesting;

    std::istringstream stream(output);
    std::string line;
    while (std::getline(stream, line)) {
        line = trim(line);
        if (line.empty()) {
            continue;
        }

        lines.push_back(line);
        const std::string lower = to_lower(line);
        if (lower.find("fail") != std::string::npos ||
            lower.find("error") != std::string::npos ||
            lower.find("elapsed time") != std::string::npos ||
            lower.find("case") != std::string::npos) {
            interesting.push_back(line);
        }
    }

    if (lines.empty()) {
        return "no submit output";
    }

    const std::vector<std::string> &source =
        interesting.empty() ? lines : interesting;
    const std::size_t begin = source.size() > 3 ? source.size() - 3 : 0;

    std::ostringstream summary;
    for (std::size_t i = begin; i < source.size(); ++i) {
        if (summary.tellp() > 0) {
            summary << " | ";
        }
        summary << source[i];
    }

    const std::string text = summary.str();
    return text.size() > 240 ? text.substr(0, 240) : text;
}

std::string zip_name_for_revision(int revision)
{
    return CONFIG.zip_prefix + "_" + std::to_string(revision) + ".zip";
}

bool compress_to_zip(int revision)
{
    if (!path_exists(CONFIG.bin_src)) {
        ci_log("FAIL binary not found");
        ci_debug("binary not found: " + CONFIG.bin_src);
        return false;
    }
    if (!mkdir_p(CONFIG.zip_dir)) {
        ci_debug("failed to create zip directory: " + CONFIG.zip_dir);
        return false;
    }

    const std::string zip_path =
        join_path(CONFIG.zip_dir, zip_name_for_revision(revision));
    const std::string tmp_zip_path = zip_path + ".tmp.zip";
    ::unlink(tmp_zip_path.c_str());

    const std::string command =
        "zip -j -q " + shell_quote(tmp_zip_path) + " " +
        shell_quote(CONFIG.bin_src);
    const CommandResult result = run_command(command, "", false, true);
    if (!result.ok) {
        ::unlink(tmp_zip_path.c_str());
        return false;
    }

    if (::rename(tmp_zip_path.c_str(), zip_path.c_str()) != 0) {
        ci_debug(
            "failed to rename temporary zip: " +
            std::string(std::strerror(errno)));
        ::unlink(tmp_zip_path.c_str());
        return false;
    }
    return true;
}

struct ZipEntry {
    std::string path;
    int revision;
    std::time_t modified_time;
};

bool parse_zip_revision(const std::string &name, int *revision)
{
    const std::string prefix = CONFIG.zip_prefix + "_";
    const std::string suffix = ".zip";
    if (name.size() <= prefix.size() + suffix.size() ||
        name.compare(0, prefix.size(), prefix) != 0 ||
        name.compare(name.size() - suffix.size(), suffix.size(), suffix) != 0) {
        return false;
    }

    const std::string number = name.substr(
        prefix.size(),
        name.size() - prefix.size() - suffix.size());
    char *end = NULL;
    errno = 0;
    const long parsed = std::strtol(number.c_str(), &end, 10);
    if (errno != 0 || end == number.c_str() || *end != '\0') {
        return false;
    }
    *revision = static_cast<int>(parsed);
    return true;
}

void clean_old_zips()
{
    if (CONFIG.max_bin_keep <= 0 || !is_directory(CONFIG.zip_dir)) {
        return;
    }

    DIR *directory = ::opendir(CONFIG.zip_dir.c_str());
    if (directory == NULL) {
        ci_debug("failed to open zip directory: " + CONFIG.zip_dir);
        return;
    }

    std::vector<ZipEntry> entries;
    struct dirent *item = NULL;
    while ((item = ::readdir(directory)) != NULL) {
        const std::string name(item->d_name);
        int revision = -1;
        if (!parse_zip_revision(name, &revision)) {
            continue;
        }

        const std::string path = join_path(CONFIG.zip_dir, name);
        struct stat st;
        if (::stat(path.c_str(), &st) != 0) {
            continue;
        }

        ZipEntry entry;
        entry.path = path;
        entry.revision = revision;
        entry.modified_time = st.st_mtime;
        entries.push_back(entry);
    }
    ::closedir(directory);

    std::sort(
        entries.begin(),
        entries.end(),
        [](const ZipEntry &left, const ZipEntry &right) {
            if (left.revision != right.revision) {
                return left.revision < right.revision;
            }
            return left.modified_time < right.modified_time;
        });

    if (entries.size() <= static_cast<std::size_t>(CONFIG.max_bin_keep)) {
        return;
    }

    const std::size_t remove_count =
        entries.size() - static_cast<std::size_t>(CONFIG.max_bin_keep);
    for (std::size_t i = 0; i < remove_count; ++i) {
        if (::unlink(entries[i].path.c_str()) != 0) {
            ci_debug("failed to remove old zip: " + entries[i].path);
        }
    }
}

const int STATUS_WIDTH = 7;
const int REVISION_WIDTH = 8;

std::string make_record(
    const std::string &status,
    int revision,
    const std::string &author,
    const std::string &reason)
{
    const std::string safe_author = trim(author).empty() ? "unknown" : trim(author);
    const std::string safe_reason = trim(reason).empty() ? "ok" : trim(reason);
    const std::string status_text =
        to_upper(status) == "SUCCESS" ? "Success" : "FAIL";
    const std::string revision_text = "r" + std::to_string(revision);

    std::ostringstream record;
    record << std::left
           << std::setw(STATUS_WIDTH) << status_text << "  "
           << std::setw(REVISION_WIDTH) << revision_text << "  "
           << std::setw(CONFIG.author_width) << safe_author << "  "
           << std::setw(CONFIG.reason_width) << safe_reason << "  "
           << '[' << now_string() << ']';
    return record.str();
}

enum LineStatus {
    LINE_SUCCESS,
    LINE_FAIL,
    LINE_OTHER
};

LineStatus line_status(const std::string &line)
{
    const std::string upper = to_upper(trim(line));
    if (upper.compare(0, 7, "SUCCESS") == 0) {
        return LINE_SUCCESS;
    }
    if (upper.compare(0, 4, "FAIL") == 0) {
        return LINE_FAIL;
    }
    if ((" " + upper + " ").find(" FAIL ") != std::string::npos) {
        return LINE_FAIL;
    }
    return LINE_OTHER;
}

int line_revision(const std::string &line)
{
    std::smatch match;
    static const std::regex revision_pattern("\\br([0-9]+)\\b");
    if (std::regex_search(line, match, revision_pattern)) {
        return std::atoi(match[1].str().c_str());
    }

    static const std::regex version_pattern("\\bversion=([0-9]+)\\b");
    if (std::regex_search(line, match, version_pattern)) {
        return std::atoi(match[1].str().c_str());
    }
    return -1;
}

void update_mk_fail_success(
    int revision,
    const std::string &author)
{
    const std::vector<std::string> old_lines =
        read_nonempty_lines(CONFIG.mk_fail_file);
    std::vector<std::string> new_lines;
    new_lines.push_back(make_record("SUCCESS", revision, author, "ok"));

    for (std::size_t i = 0; i < old_lines.size(); ++i) {
        if (line_status(old_lines[i]) == LINE_FAIL &&
            line_revision(old_lines[i]) != revision) {
            new_lines.push_back(old_lines[i]);
        }
    }

    if (!write_lines(CONFIG.mk_fail_file, new_lines)) {
        ci_debug("failed to update mk_fail: " + CONFIG.mk_fail_file);
    }
}

void update_mk_fail_failure(
    int revision,
    const std::string &author,
    const std::string &reason)
{
    const std::vector<std::string> old_lines =
        read_nonempty_lines(CONFIG.mk_fail_file);

    std::string success_line;
    std::vector<std::string> fail_lines;
    for (std::size_t i = 0; i < old_lines.size(); ++i) {
        const LineStatus status = line_status(old_lines[i]);
        if (status == LINE_SUCCESS && success_line.empty()) {
            success_line = old_lines[i];
        } else if (status == LINE_FAIL &&
                   line_revision(old_lines[i]) != revision) {
            fail_lines.push_back(old_lines[i]);
        }
    }

    std::vector<std::string> new_lines;
    if (!success_line.empty()) {
        new_lines.push_back(success_line);
    }
    new_lines.push_back(make_record("FAIL", revision, author, reason));
    new_lines.insert(new_lines.end(), fail_lines.begin(), fail_lines.end());

    if (!write_lines(CONFIG.mk_fail_file, new_lines)) {
        ci_debug("failed to update mk_fail: " + CONFIG.mk_fail_file);
    }
}

void record_failure(
    int revision,
    const std::string &author,
    const std::string &reason)
{
    update_mk_fail_failure(revision, author, reason);
    if (!save_last_version(revision)) {
        ci_debug("failed to save last_version");
    }
}

void record_success(int revision, const std::string &author)
{
    update_mk_fail_success(revision, author);
    if (!save_last_version(revision)) {
        ci_debug("failed to save last_version");
    }
}

bool build_revision(int revision)
{
    ci_log("build r" + std::to_string(revision));

    const std::string author = get_author(revision);
    ci_debug(
        "r" + std::to_string(revision) + " author=" + author);

    svn_clean();

    if (!checkout(revision)) {
        ci_log("FAIL r" + std::to_string(revision));
        ci_debug(
            "svn update failed for r" + std::to_string(revision));
        record_failure(revision, author, "svn_update_failed");
        return false;
    }

    bool mk_ok = build();
    if (!mk_ok) {
        ci_debug(
            "r" + std::to_string(revision) +
            " first mk failed, retry after make clean");
        make_clean();
        mk_ok = build();
    }

    if (!mk_ok) {
        ci_log("FAIL r" + std::to_string(revision) + " mk_failed");
        record_failure(revision, author, "mk_failed_after_retry");
        return false;
    }

    ci_log("submit_test r" + std::to_string(revision));
    const SubmitResult submit = run_submit_test();
    if (!submit.ok) {
        const std::string detail = summarize_submit_output(submit.output);
        ci_log("FAIL r" + std::to_string(revision) + " submit_failed");
        ci_debug(
            "submit failed for r" + std::to_string(revision) + ": " +
            submit.reason + ": " + detail);
        record_failure(
            revision,
            author,
            "submit_failed:" + submit.reason + ":" + detail);
        return false;
    }

    if (!compress_to_zip(revision)) {
        ci_log("FAIL r" + std::to_string(revision) + " compress_failed");
        ci_debug(
            "compress failed for r" + std::to_string(revision));
        record_failure(revision, author, "compress_failed");
        return false;
    }

    clean_old_zips();
    record_success(revision, author);
    ci_log("Success r" + std::to_string(revision));
    return true;
}

void print_startup()
{
    ci_log("SVN build watcher started");
    ci_debug("WORK_DIR=" + CONFIG.work_dir);
    ci_debug("BIN_SRC=" + CONFIG.bin_src);
    ci_debug("BIN_DST=" + CONFIG.bin_dst);
    ci_debug("ZIP_DIR=" + CONFIG.zip_dir);
    ci_debug("SUBMIT_TEST_DIR=" + CONFIG.submit_test_dir);
    ci_debug("SUBMIT_TEST_SCRIPT=" + CONFIG.submit_test_script);
    ci_debug("MK_FAIL_FILE=" + CONFIG.mk_fail_file);
    ci_debug("LAST_VERSION_FILE=" + CONFIG.last_version_file);
    ci_debug(
        "Starting version: r" + std::to_string(load_last_version()));
}

bool validate_paths()
{
    bool ok = true;
    if (!mkdir_p(CONFIG.bin_dst)) {
        ci_log("failed to create BIN_DST: " + CONFIG.bin_dst);
        ok = false;
    }
    if (!mkdir_p(CONFIG.zip_dir)) {
        ci_log("failed to create ZIP_DIR: " + CONFIG.zip_dir);
        ok = false;
    }
    if (!is_directory(CONFIG.work_dir)) {
        ci_debug("warning: WORK_DIR does not exist: " + CONFIG.work_dir);
    }
    return ok;
}

}  // namespace

int main()
{
    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);

    if (!validate_paths()) {
        return 1;
    }
    print_startup();

    while (g_running) {
        const int head = get_head();
        const int local = load_last_version();

        if (head < 0) {
            ci_log("failed to get SVN HEAD, retry later");
            ::sleep(10);
            continue;
        }

        if (head <= local) {
            const std::time_t wait_start = std::time(NULL);
            while (g_running) {
                const int current_head = get_head();
                if (current_head > local) {
                    break;
                }

                const int elapsed = static_cast<int>(
                    std::time(NULL) - wait_start);
                std::cout << "\r[CI] Idle | local=" << local
                          << " head=" << current_head
                          << " wait: " << elapsed << "s"
                          << std::flush;
                ::sleep(CONFIG.idle_sleep > 0 ? CONFIG.idle_sleep : 1);
            }
            std::cout << std::endl;
            continue;
        }

        ci_log(
            "update detected " + std::to_string(local) +
            " -> " + std::to_string(head));

        for (int revision = local + 1;
             revision <= head && g_running;
             ++revision) {
            build_revision(revision);
        }

        ::sleep(CONFIG.poll_interval > 0 ? CONFIG.poll_interval : 1);
    }

    ci_log("exit");
    return 0;
}
