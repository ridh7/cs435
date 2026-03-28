#include <iostream>
#include <fstream>
#include <sys/socket.h>
#include <netdb.h>
#include <unistd.h>
using namespace std;

int main(int argc, char *argv[])
{

    if (argc != 2)
    {
        cerr << "usage: ./http_client <url>\n";
        return 1;
    }

    ofstream file("output", ios::binary);

    string url;
    url = argv[1];

    if (url.find("http://") != 0)
    {
        file << "INVALIDPROTOCOL\n";
        return 1;
    }

    string sub = url.substr(7);

    int port = 80;
    size_t colonIdx = sub.find(":");
    size_t slashIdx = sub.find("/");

    if (colonIdx != string::npos)
    {
        size_t startIdx = colonIdx + 1;
        if (slashIdx != string::npos) // slash present, read up to the slash
            port = stoi(sub.substr(startIdx, slashIdx - startIdx));
        else if (colonIdx + 1 != sub.length()) // slash not present, read till EOS
            port = stoi(sub.substr(startIdx));
    }

    string hostname = sub.substr(0, min(colonIdx, slashIdx)); // read till the port number (if present), else till the slash (if present), else till EOS
    string path;

    if (slashIdx != string::npos)
        path = sub.substr(slashIdx); // if slash present read starting from the slash
    else
        path = "/";

    struct addrinfo hints, *res, *p;
    hints = {};                      // set all fields to 0/NULL
    hints.ai_family = AF_UNSPEC;     // accept both IPv4 and IPv6
    hints.ai_socktype = SOCK_STREAM; // TCP

    if (getaddrinfo(hostname.c_str(), to_string(port).c_str(), &hints, &res) != 0)
    {
        file << "NOCONNECTION\n";
        return 1;
    }

    int sockfd;

    for (p = res; p != NULL; p = p->ai_next)
    {
        if ((sockfd = socket(p->ai_family, p->ai_socktype, p->ai_protocol)) == -1)
            continue;
        // TCP handshake
        if (connect(sockfd, p->ai_addr, p->ai_addrlen) == -1) // ai_addr is a pointer to the destination address struct, ai_addrlen is the size of ai_addr
        {
            close(sockfd);
            continue;
        }
        break;
    }

    if (p == NULL)
    {
        file << "NOCONNECTION\n";
        return 1;
    }

    freeaddrinfo(res);

    string req = "GET " + path + " HTTP/1.0\r\nHost: " + hostname + "\r\n\r\n"; // \r\n is HTTP standard

    send(sockfd, req.c_str(), req.length(), 0);

    string resp;

    while (true)
    {
        char buffer[4096]; // standard memory page size
        int numBytes = recv(sockfd, buffer, sizeof(buffer), 0);
        if (numBytes <= 0)
            break;
        resp.append(buffer, numBytes);
    }

    string body = resp.substr(resp.find("\r\n\r\n") + 4);
    int statusCode = stoi(resp.substr(resp.find(" ") + 1));

    if (statusCode == 404)
    {
        file << "FILENOTFOUND\n";
        return 1;
    }

    file.write(body.data(), body.length());

    return 0;
}