/* Win98 toolchain smoke test: minimal native GUI app.
 * Goal is not the window, it is the PE header and import table it produces. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>

static const char CLS[] = "Hello98Wnd";

static LRESULT CALLBACK WndProc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    PAINTSTRUCT ps;
    HDC dc;
    RECT rc;

    switch (m) {
    case WM_PAINT:
        dc = BeginPaint(h, &ps);
        GetClientRect(h, &rc);
        SelectObject(dc, GetStockObject(ANSI_VAR_FONT));
        DrawTextA(dc, "Claude Code, eventually.", -1, &rc,
                  DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        EndPaint(h, &ps);
        return 0;
    case WM_DESTROY:
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcA(h, m, w, l);
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    WNDCLASSA wc;
    HWND hwnd;
    MSG msg;

    (void)prev; (void)cmd;

    ZeroMemory(&wc, sizeof(wc));
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = inst;
    wc.hCursor       = LoadCursorA(NULL, IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = CLS;
    if (!RegisterClassA(&wc)) return 1;

    hwnd = CreateWindowExA(0, CLS, "Hello, Windows 98",
                           WS_OVERLAPPEDWINDOW, CW_USEDEFAULT, CW_USEDEFAULT,
                           360, 180, NULL, NULL, inst, NULL);
    if (!hwnd) return 1;

    ShowWindow(hwnd, show);
    UpdateWindow(hwnd);

    while (GetMessageA(&msg, NULL, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    return (int)msg.wParam;
}
