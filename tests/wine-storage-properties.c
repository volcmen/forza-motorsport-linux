/* SPDX-FileCopyrightText: 2026 Forza Motorsport Linux tools contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Run only against the dedicated Forza Wine candidate, in a disposable prefix.
 */
#include <windows.h>
#include <winioctl.h>
#include <stdio.h>

static unsigned int checks, failures;
#define CHECK(condition) do { checks++; if (!(condition)) { \
    fprintf(stderr, "FAIL line %u: %s (error %lu)\n", __LINE__, #condition, GetLastError()); failures++; \
} } while (0)

int main(void)
{
    HANDLE volume = CreateFileA("\\\\.\\C:", 0, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                NULL, OPEN_EXISTING, 0, NULL);
    STORAGE_PROPERTY_QUERY query = {0};
    DEVICE_TRIM_DESCRIPTOR trim;
    DEVICE_SEEK_PENALTY_DESCRIPTOR seek;
    DWORD returned;
    BOOL result;

    CHECK(volume != INVALID_HANDLE_VALUE);
    if (volume == INVALID_HANDLE_VALUE) return 1;
    query.PropertyId = StorageDeviceTrimProperty;
    query.QueryType = PropertyStandardQuery;
    memset(&trim, 0xcc, sizeof(trim));
    result = DeviceIoControl(volume, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                             &trim, sizeof(trim), &returned, NULL);
    CHECK(result);
    CHECK(returned == sizeof(trim));
    CHECK(trim.Version == sizeof(trim));
    CHECK(trim.Size == sizeof(trim));
    CHECK(trim.TrimEnabled == TRUE);

    memset(&trim, 0xcc, sizeof(trim));
    result = DeviceIoControl(volume, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                             &trim, sizeof(STORAGE_DESCRIPTOR_HEADER), &returned, NULL);
    CHECK(result);
    CHECK(returned == sizeof(STORAGE_DESCRIPTOR_HEADER));
    CHECK(trim.Version == sizeof(trim));
    CHECK(trim.Size == sizeof(trim));
    CHECK(trim.TrimEnabled == 0xcc); /* No write beyond the requested header. */

    result = DeviceIoControl(volume, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                             &trim, sizeof(STORAGE_DESCRIPTOR_HEADER) - 1, &returned, NULL);
    CHECK(!result);
    result = DeviceIoControl(volume, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query) - 1,
                             &trim, sizeof(trim), &returned, NULL);
    CHECK(!result);

    query.PropertyId = StorageDeviceSeekPenaltyProperty;
    memset(&seek, 0xcc, sizeof(seek));
    result = DeviceIoControl(volume, IOCTL_STORAGE_QUERY_PROPERTY, &query, sizeof(query),
                             &seek, sizeof(seek), &returned, NULL);
    CHECK(result);
    CHECK(returned == sizeof(seek));
    CHECK(seek.Version == sizeof(seek));
    CHECK(seek.Size == sizeof(seek));
    CHECK(seek.IncursSeekPenalty == FALSE);
    CloseHandle(volume);
    printf("storage properties: %u checks, %u failures\n", checks, failures);
    return failures ? 1 : 0;
}
