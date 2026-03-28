#include <iostream>
#include <sys/socket.h>
#include <netdb.h>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <fstream>
using namespace std;

// remove zombie child processes
void sigchld_handler(int s)
{
    while (waitpid(-1, NULL, WNOHANG) > 0)
        ;
}

int main(int argc, char *argv[])
{
    if (argc != 2)
    {
        cerr << "usage: ./http_server <port>\n";
        return 1;
    }

    char *port = argv[1];

    struct addrinfo hints, *res, *p;

    hints = {};
    hints.ai_family = AF_UNSPEC;     // accept both IPv4 and IPv6
    hints.ai_socktype = SOCK_STREAM; // TCP
    hints.ai_flags = AI_PASSIVE;     // accept connections on any network interface

    if (getaddrinfo(NULL, port, &hints, &res) != 0)
    {
        cerr << "Error in getaddrinfo\n";
        return 1;
    }

    int sockfd, yes = 1;

    for (p = res; p != NULL; p = p->ai_next)
    {
        if ((sockfd = socket(p->ai_family, p->ai_socktype, p->ai_protocol)) == -1)
            continue;

        // SO_REUSEADDR lets you immediately rebind to the same port.
        if (setsockopt(sockfd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes)) == -1)
            continue;
        // assign IP + port to the socket
        if (::bind(sockfd, p->ai_addr, p->ai_addrlen) == -1)
        {
            close(sockfd);
            continue;
        }
        break;
    }

    if (p == NULL)
    {
        cerr << "Error in binding\n";
        return 1;
    }

    freeaddrinfo(res);

    // 10 clients queues, rest turned away
    if (listen(sockfd, 10) == -1)
    {
        cerr << "Error listening\n";
        return 1;
    }

    struct sigaction sa;
    sa.sa_handler = sigchld_handler;
    // no additional signals are blocked during handler execution, SIGCHLD itself is automatically blocked
    sigemptyset(&sa.sa_mask);
    // automatically restart interrupted system calls
    sa.sa_flags = SA_RESTART;
    // register handler
    sigaction(SIGCHLD, &sa, NULL);

    while (true)
    {
        // store client address
        struct sockaddr_storage their_addr;
        socklen_t sin_size = sizeof(their_addr);
        int new_fd = accept(sockfd, (struct sockaddr *)&their_addr, &sin_size);
        if (new_fd == -1)
            continue;
        // two processes running in the same code
        pid_t c_pid = fork();
        if (c_pid == -1)
        {
            cerr << "Error creating child process";
            continue;
        }
        if (c_pid == 0)
        {
            close(sockfd);

            string req;
            char buffer[4096];

            int numBytes = recv(new_fd, buffer, sizeof(buffer), 0);
            req.append(buffer, numBytes);

            // sample GET request: GET /somefile.txt HTTP/1.1\r\n...
            string sub = req.substr(req.find(" ") + 1);
            string path = sub.substr(0, sub.find(" "));
            // relative path
            path = path.substr(1);

            ifstream file(path, ios::binary);

            string header = "HTTP/1.1 404 Not Found\r\n\r\n";

            if (!file.is_open())
            {
                send(new_fd, header.c_str(), header.length(), 0);
            }
            else
            {
                // jump to the end
                file.seekg(0, ios::end);
                // get file size
                size_t fileSize = file.tellg();
                // rewind to the start
                file.seekg(0, ios::beg);
                header = "HTTP/1.1 200 OK\r\nContent-Length: " + to_string(fileSize) + "\r\n\r\n";
                send(new_fd, header.c_str(), header.length(), 0);
                char buf[4096];
                while (file.read(buf, sizeof(buf)) || file.gcount() > 0) // file.gcount() -> number of bytes that were read
                    send(new_fd, buf, file.gcount(), 0);
            }
            close(new_fd);
            exit(0);
        }
        close(new_fd);
    }

    return 0;
}