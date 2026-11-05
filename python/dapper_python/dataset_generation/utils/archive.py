"""
Based on the kinds of files we're scraping for this project (official package archives from Ubuntu, Debian, etc),
We should hopefully not encounter malicious archives, but we should try ot be as safe as possible anyway
"""

from __future__ import annotations

from pathlib import Path
from tarfile import TarFile, TarInfo
from zipfile import ZipFile, ZipInfo

from typing import Union


class SafeTarFile(TarFile):
    def safe_extractall(self, path: Union[Path, str], **kwargs) -> None:
        """Extracts all archive to a given path

        Does some additional checking to try to prevent malicious tarfile contents from being extracted
        Such as files that use absolute paths or paths containing ".." to try and modify files outside the target directory

        Intended as an improved-safety version of extractall() for compatability with older Python versions,
        But a better approach is to use a newer Python version which has better security built in to extractall()
        However, this library currently supports older python versions that don't have that built in
        """
        if isinstance(path, str):
            path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"No such directory: {path}")
        elif not path.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        path = path.resolve()

        kwargs.pop("member", None)
        kwargs.pop("path", None)
        for member in self.getmembers():
            output_path = path.joinpath(member.name).resolve()

            if not output_path.is_relative_to(path):
                # Do not extract any file that would be placed outside the provided root path
                continue

            if member.issym() or member.islnk():
                link_target = Path(member.linkname)
                if link_target.is_absolute():
                    # Exclude all absolute symlinks
                    continue

                link_target = output_path.parent.joinpath(link_target).resolve()
                if not link_target.is_relative_to(path):
                    # Exclude any symlink whose target would be outside the provided root path
                    continue

            self.extract(member, path=path, **kwargs)


class SafeZipFile(ZipFile):
    def safe_extractall(self, path: Union[Path, str], **kwargs) -> None:
        """Extracts all archive members to a given path

        Does some additional checking to try to prevent malicious tarfile contents from being extracted
        Such as files that use absolute paths or paths containing ".." to try and modify files outside the target directory
        """
        if isinstance(path, str):
            path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"No such directory: {path}")
        elif not path.is_dir():
            raise NotADirectoryError(f"Not a directory: {path}")
        path = path.resolve()

        kwargs.pop("member", None)
        kwargs.pop("path", None)
        for member in self.namelist():
            output_path = path.joinpath(member).resolve()
            if not output_path.is_relative_to(path):
                # Do not extract any file that would be placed outside the provided root path
                continue

            member_info = self.getinfo(member)
            if self.is_symlink(member_info):
                link_target = Path(self.open(member_info).read().decode("utf-8"))
                if link_target.is_absolute():
                    # Exclude all absolute symlinks
                    continue

                link_target = output_path.parent.joinpath(link_target).resolve()
                if not link_target.is_relative_to(path):
                    # Exclude any symlink whose target would be outside the provided root path
                    continue

            self.extract(member, path=path, **kwargs)

    @staticmethod
    def is_symlink(zip_info: ZipInfo) -> bool:
        mode = (zip_info.external_attr >> 16) & 0xFFFF
        return (mode & 0o170000) == 0o120000
