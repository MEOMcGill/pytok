from distutils.core import setup
import os.path
import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="pytok",
    packages=setuptools.find_packages(),
    version="0.0.2",
    license="MIT",
    description="TikTok scraper with automatic captcha solving using zendriver browser automation.",
    author="Ben Steel",
    author_email="bendavidsteel@gmail.com",
    url="https://github.com/networkdynamics/pytok",
    long_description=long_description,
    long_description_content_type="text/markdown",
    keywords=[
        "tiktok",
        "tiktok-scraper",
        "tiktok-api",
        "web-scraping",
        "social-media",
        "data-collection",
        "browser-automation",
        "captcha-solver",
        "async",
        "research",
    ],
    install_requires=[
        "zendriver",
        "requests",
        "httpx",
        "brotli",
        "opencv-python",
        "numpy",
        "tqdm",
        "pandas",
        "polars",
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "Topic :: Internet :: WWW/HTTP :: Indexing/Search",
        "Topic :: Scientific/Engineering :: Information Analysis",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.9",
)
